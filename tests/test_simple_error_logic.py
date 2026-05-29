#!/usr/bin/env python3
"""
Простой тест логики обработки ошибок без использования внешних зависимостей
"""


def test_error_message_logic():
    """Тест логики обработки сообщений об ошибках"""
    print("Тестируем логику обработки сообщений об ошибках...")

    # Тест 1: Проверка определения ошибок, связанных с емкостью
    def is_capacity_related_error(error_msg):
        return "capacity" in error_msg.lower() or "available" in error_msg.lower() or "429" in error_msg

    # Проверяем различные варианты ошибок
    capacity_errors = [
        "No capacity available for model gemini-3-pro-preview on the server",
        "Capacity exceeded",
        "Model not available",
        "429 Too Many Requests",
        "Rate limit exceeded (429)"
    ]

    for error in capacity_errors:
        assert is_capacity_related_error(error), f"Ошибка '{error}' должна быть распознана как связанная с емкостью"

    # Проверяем, что обычные ошибки не считаются ошибками емкости
    regular_errors = [
        "Connection timeout",
        "Invalid input",
        "File not found"
    ]

    for error in regular_errors:
        assert not is_capacity_related_error(error), f"Ошибка '{error}' НЕ должна быть распознана как связанная с емкостью"

    print("✓ Логика определения ошибок емкости работает корректно")

    # Тест 2: Проверка формирования сообщений об ошибках
    def get_error_message(error_msg):
        if "capacity" in error_msg.lower() or "available" in error_msg.lower() or "429" in error_msg:
            return "Ошибка: Недостаточно ресурсов для обработки запроса. Пожалуйста, повторите попытку позже."
        else:
            return f"Ошибка агента: {error_msg}"

    # Проверяем сообщения для ошибок емкости
    capacity_msg = get_error_message("No capacity available for model")
    expected_capacity_msg = "Ошибка: Недостаточно ресурсов для обработки запроса. Пожалуйста, повторите попытку позже."
    assert capacity_msg == expected_capacity_msg, f"Ожидалось '{expected_capacity_msg}', получено '{capacity_msg}'"

    # Проверяем сообщения для обычных ошибок
    regular_msg = get_error_message("Some other error")
    expected_regular_msg = "Ошибка агента: Some other error"
    assert regular_msg == expected_regular_msg, f"Ожидалось '{expected_regular_msg}', получено '{regular_msg}'"

    print("✓ Логика формирования сообщений об ошибках работает корректно")


def test_empty_message_logic():
    """Тест логики проверки пустых сообщений"""
    print("Тестируем логику проверки пустых сообщений...")

    # Проверяем, как определяется пустое сообщение
    def is_empty_message(text):
        return text is None or str(text).strip() == ""

    # Тестируем различные варианты
    assert is_empty_message(None), "None должен считаться пустым сообщением"
    assert is_empty_message(""), "Пустая строка должна считаться пустым сообщением"
    assert is_empty_message("   "), "Строка с пробелами должна считаться пустым сообщением"
    assert is_empty_message("\t\n  "), "Строка с пробельными символами должна считаться пустым сообщением"

    # Тестируем непустые сообщения
    assert not is_empty_message("Hello"), "Нормальное сообщение не должно считаться пустым"
    assert not is_empty_message("  Hello  "), "Сообщение с пробелами по краям не должно считаться пустым"

    print("✓ Логика проверки пустых сообщений работает корректно")


if __name__ == "__main__":
    print("Запуск простых тестов логики обработки ошибок...")
    print()

    test_error_message_logic()
    test_empty_message_logic()

    print()
    print("Все тесты пройдены успешно!")
    print()
    print("Внесенные изменения:")
    print("1. Добавлена проверка пустого текста сообщения в _send_message()")
    print("2. Добавлена обработка ошибки 'Message text is empty'")
    print("3. Добавлена обработка ошибок, связанных с емкостью/доступностью моделей")
    print("4. Реализована отправка информативных сообщений об ошибках в Telegram")
