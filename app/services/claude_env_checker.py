"""
Модуль проверки окружения для запуска CLI-агентов от имени claude-bot.

Проверяет:
1. Существование пользователя claude-bot
2. Наличие установленного claude у пользователя
3. Права на workdir
4. Доступность ~/.local/bin в PATH
"""

import subprocess
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Tuple

logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    """Результат отдельной проверки."""
    name: str
    passed: bool
    message: str
    details: Optional[str] = None


@dataclass
class EnvCheckResult:
    """Общий результат проверки окружения."""
    all_passed: bool
    user_exists: bool = False
    claude_installed: bool = False
    workdir_accessible: bool = False
    path_configured: bool = False
    claude_version: Optional[str] = None
    checks: List[CheckResult] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def is_claude_available(self) -> bool:
        """Проверяет, готово ли окружение для запуска claude."""
        return (
            self.user_exists
            and self.claude_installed
            and self.workdir_accessible
            and self.path_configured
        )


class ClaudeEnvChecker:
    """
    Проверка окружения для запуска CLI-агентов от имени claude-bot.

    Использование:
        checker = ClaudeEnvChecker(workdir="/srv/git_projects", username="claude-bot")
        result = checker.check_all()
        if result.is_claude_available():
            # claude готов к использованию
        else:
            # вывести ошибки пользователю
    """

    def __init__(
        self,
        workdir: str,
        username: str = "claude-bot",
        claude_binary: str = "claude",
    ):
        self.workdir = workdir
        self.username = username
        self.claude_binary = claude_binary
        self.user_home = f"/home/{username}"

    def _run_as_user(self, command: str) -> Tuple[bool, str, str]:
        """
        Выполнить команду от имени пользователя.

        Returns:
            (success, stdout, stderr)
        """
        try:
            result = subprocess.run(
                ["su", "-", self.username, "-c", command],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return (
                result.returncode == 0,
                result.stdout.strip(),
                result.stderr.strip(),
            )
        except subprocess.TimeoutExpired:
            return False, "", "Timeout"
        except FileNotFoundError:
            return False, "", "Команда 'su' не найдена"
        except Exception as e:
            return False, "", str(e)

    def check_user_exists(self) -> CheckResult:
        """Проверить существование пользователя."""
        try:
            result = subprocess.run(
                ["id", self.username],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                # Получаем домашнюю директорию
                success, home, _ = self._run_as_user("echo $HOME")
                if success:
                    self.user_home = home

                return CheckResult(
                    name="user_exists",
                    passed=True,
                    message=f"Пользователь '{self.username}' существует",
                    details=result.stdout.strip(),
                )
            else:
                return CheckResult(
                    name="user_exists",
                    passed=False,
                    message=f"Пользователь '{self.username}' не найден",
                )
        except subprocess.TimeoutExpired:
            return CheckResult(
                name="user_exists",
                passed=False,
                message="Таймаут при проверке пользователя",
            )
        except FileNotFoundError:
            return CheckResult(
                name="user_exists",
                passed=False,
                message="Команда 'id' не найдена",
            )
        except Exception as e:
            logger.exception("Ошибка при проверке пользователя")
            return CheckResult(
                name="user_exists",
                passed=False,
                message=f"Ошибка: {e}",
            )

    def check_claude_installed(self) -> CheckResult:
        """Проверить наличие установленного claude."""
        command = f"command -v {self.claude_binary}"
        success, stdout, stderr = self._run_as_user(command)

        if success and stdout:
            binary_path = stdout
            # Получаем версию
            version_success, version_out, _ = self._run_as_user(
                f"{self.claude_binary} --version"
            )
            version = version_out if version_success else None

            return CheckResult(
                name="claude_installed",
                passed=True,
                message=f"claude установлен: {binary_path}",
                details=version,
            )
        else:
            return CheckResult(
                name="claude_installed",
                passed=False,
                message="claude не найден в PATH пользователя",
                details=stderr if stderr else None,
            )

    def check_workdir_accessible(self) -> CheckResult:
        """Проверить доступность workdir для записи."""
        command = f"test -w '{self.workdir}' && echo 'OK'"
        success, stdout, _ = self._run_as_user(command)

        if success and "OK" in stdout:
            return CheckResult(
                name="workdir_accessible",
                passed=True,
                message=f"Директория '{self.workdir}' доступна для записи",
            )
        else:
            return CheckResult(
                name="workdir_accessible",
                passed=False,
                message=f"Нет доступа на запись в '{self.workdir}'",
            )

    def check_path_configured(self) -> CheckResult:
        """Проверить, что ~/.local/bin в PATH."""
        command = 'echo $PATH'
        success, stdout, _ = self._run_as_user(command)

        local_bin_path = f"{self.user_home}/.local/bin"
        if success and local_bin_path in stdout:
            return CheckResult(
                name="path_configured",
                passed=True,
                message="~/.local/bin присутствует в PATH",
            )
        else:
            # Альтернативная проверка: найти claude в ~/.local/bin
            check_bin = f"test -x {self.user_home}/.local/bin/{self.claude_binary} && echo 'OK'"
            bin_success, bin_stdout, _ = self._run_as_user(check_bin)

            if bin_success and "OK" in bin_stdout:
                return CheckResult(
                    name="path_configured",
                    passed=True,
                    message=f"~/.local/bin/{self.claude_binary} существует",
                    details="PATH может не содержать ~/.local/bin, но бинарник доступен",
                )

            return CheckResult(
                name="path_configured",
                passed=False,
                message="~/.local/bin не найден в PATH",
                details=f"Ожидаемый путь: {local_bin_path}",
            )

    def check_all(self) -> EnvCheckResult:
        """
        Выполнить все проверки.

        Returns:
            EnvCheckResult с результатами всех проверок
        """
        # Обновляем user_home, если пользователь существует
        if self.username != "claude-bot":
            # Для кастомного username
            self.user_home = f"/home/{self.username}"

        checks = [
            self.check_user_exists(),
            self.check_claude_installed(),
            self.check_workdir_accessible(),
            self.check_path_configured(),
        ]

        # Извлекаем флаги
        user_exists = checks[0].passed
        claude_installed = checks[1].passed
        workdir_accessible = checks[2].passed
        path_configured = checks[3].passed

        # Получаем версию claude
        claude_version = None
        if claude_installed:
            version_success, version_out, _ = self._run_as_user(
                f"{self.claude_binary} --version"
            )
            if version_success:
                claude_version = version_out

        # Собираем ошибки
        errors = []
        for check in checks:
            if not check.passed:
                errors.append(f"{check.name}: {check.message}")

        all_passed = all(c.passed for c in checks)

        return EnvCheckResult(
            all_passed=all_passed,
            user_exists=user_exists,
            claude_installed=claude_installed,
            workdir_accessible=workdir_accessible,
            path_configured=path_configured,
            claude_version=claude_version,
            checks=checks,
            errors=errors,
        )


def check_claude_env(workdir: str, username: str = "claude-bot") -> EnvCheckResult:
    """
    Удобная функция для проверки окружения claude.

    Args:
        workdir: Рабочая директория из конфига
        username: Имя пользователя для запуска

    Returns:
        EnvCheckResult с результатами проверок
    """
    checker = ClaudeEnvChecker(workdir=workdir, username=username)
    return checker.check_all()


def format_check_result(result: EnvCheckResult) -> str:
    """
    Отформатировать результат проверки для вывода пользователю.

    Returns:
        Читаемый текст с результатами проверок
    """
    lines = []
    lines.append("=== Проверка окружения для claude ===")

    def status_icon(ok: bool) -> str:
        return "✓" if ok else "✗"

    lines.append(f"{status_icon(result.user_exists)} Пользователь claude-bot: {'OK' if result.user_exists else 'НЕ НАЙДЕН'}")
    lines.append(f"{status_icon(result.claude_installed)} Установка claude: {'OK' if result.claude_installed else 'НЕ УСТАНОВЛЕН'}")
    lines.append(f"{status_icon(result.workdir_accessible)} Доступ к workdir: {'OK' if result.workdir_accessible else 'НЕТ ДОСТУПА'}")
    lines.append(f"{status_icon(result.path_configured)} PATH настроен: {'OK' if result.path_configured else 'НЕ НАСТРОЕН'}")

    if result.claude_version:
        lines.append(f"Версия claude: {result.claude_version}")

    if result.errors:
        lines.append("")
        lines.append("Ошибки:")
        for error in result.errors:
            lines.append(f"  - {error}")

    if result.all_passed:
        lines.append("")
        lines.append("✓ Все проверки пройдены. claude готов к использованию.")
    else:
        lines.append("")
        lines.append("Для настройки выполните:")
        lines.append("  sudo ./scripts/setup-claude-bot.sh")

    return "\n".join(lines)
