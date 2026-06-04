"""
Tests for Desktop Scheduler Panel JSON payload support.
"""

import json
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication

from desktop.widgets.scheduler_panel import SchedulerPanelWidget
from i18n import t


@pytest.fixture
def mock_facade():
    """Create mock ApplicationFacade for testing."""
    facade = MagicMock()
    facade.list_scheduler_projects.return_value = [
        {"slug": "test-project", "name": "Test Project"}
    ]
    facade.list_scheduler_notification_targets.return_value = [
        {"session_uid": "desktop:test_session", "label": "Test Session"}
    ]
    facade.list_scheduler_jobs.return_value = []
    facade.list_modes.return_value = ["agent", "analyst", "manager"]
    facade.create_scheduler_job.return_value = {
        "job_id": "test-job-1",
        "job_name": "Test Job",
        "cron": "*/5 * * * *",
        "target_mode": "agent",
        "enabled": True,
        "project_slug": "test-project",
        "notification_target": {"telegram_session_uid": "desktop:test_session"},
        "payload": {"custom_key": "custom_value", "project_slug": "test-project"},
    }
    facade.update_scheduler_job.return_value = {
        "job_id": "test-job-1",
        "job_name": "Updated Job",
        "cron": "0 0 * * *",
        "target_mode": "analyst",
        "enabled": False,
        "project_slug": "test-project",
        "notification_target": {"telegram_session_uid": "desktop:test_session"},
        "payload": {"updated_key": "updated_value", "project_slug": "test-project"},
    }
    return facade


@pytest.fixture
def scheduler_widget(mock_facade):
    """Create SchedulerPanelWidget with mock facade."""
    # Ensure QApplication exists
    app = QApplication.instance()
    if app is None:
        app = QApplication([])

    widget = SchedulerPanelWidget(facade=mock_facade)
    yield widget
    widget.close()


class TestSchedulerPanelPayload:
    """Tests for JSON payload support in Scheduler Panel."""

    def test_payload_input_widget_exists(self, scheduler_widget):
        """Verify payload QTextEdit widget is present."""
        assert hasattr(scheduler_widget, "payload_input")
        assert scheduler_widget.payload_input is not None
        assert scheduler_widget.payload_input.objectName() == "scheduler_payload_input"

    def test_payload_placeholder_text(self, scheduler_widget):
        """Verify payload input has correct placeholder."""
        placeholder = scheduler_widget.payload_input.placeholderText()
        assert placeholder == '{"key": "value"}'

    def test_reset_form_clears_payload(self, scheduler_widget):
        """Verify reset_form clears payload input."""
        scheduler_widget.payload_input.setPlainText('{"test": "value"}')
        assert scheduler_widget.payload_input.toPlainText() == '{"test": "value"}'

        scheduler_widget.reset_form()

        assert scheduler_widget.payload_input.toPlainText() == ""

    def test_collect_form_with_valid_payload(self, scheduler_widget):
        """Verify _collect_form parses valid JSON payload."""
        scheduler_widget.payload_input.setPlainText('{"custom": "data", "number": 42}')

        # Set required fields
        scheduler_widget.project_selector.setCurrentIndex(0)
        scheduler_widget.session_selector.setCurrentIndex(0)
        scheduler_widget.mode_selector.setCurrentIndex(0)

        result = scheduler_widget._collect_form()

        assert result is not None
        assert "payload" in result
        assert result["payload"]["custom"] == "data"
        assert result["payload"]["number"] == 42
        # project_slug is added by ApplicationFacade, not by UI form

    def test_collect_form_with_invalid_json(self, scheduler_widget):
        """Verify _collect_form rejects invalid JSON."""
        scheduler_widget.payload_input.setPlainText('{"invalid": json}')

        # Set required fields
        scheduler_widget.project_selector.setCurrentIndex(0)
        scheduler_widget.session_selector.setCurrentIndex(0)
        scheduler_widget.mode_selector.setCurrentIndex(0)

        result = scheduler_widget._collect_form()

        assert result is None
        assert t("desktop.scheduler.err_payload_invalid_json", "ru", error="").rstrip() in scheduler_widget.status_label.text()

    def test_collect_form_with_non_dict_payload(self, scheduler_widget):
        """Verify _collect_form rejects non-dict JSON."""
        scheduler_widget.payload_input.setPlainText('["array", "not", "object"]')

        # Set required fields
        scheduler_widget.project_selector.setCurrentIndex(0)
        scheduler_widget.session_selector.setCurrentIndex(0)
        scheduler_widget.mode_selector.setCurrentIndex(0)

        result = scheduler_widget._collect_form()

        assert result is None
        assert t("desktop.scheduler.err_payload_not_object", "ru") in scheduler_widget.status_label.text()

    def test_collect_form_with_empty_payload(self, scheduler_widget):
        """Verify _collect_form handles empty payload."""
        scheduler_widget.payload_input.clear()

        # Set required fields
        scheduler_widget.project_selector.setCurrentIndex(0)
        scheduler_widget.session_selector.setCurrentIndex(0)
        scheduler_widget.mode_selector.setCurrentIndex(0)

        result = scheduler_widget._collect_form()

        assert result is not None
        assert result["payload"] == {}

    def test_on_job_selected_populates_payload(self, scheduler_widget, mock_facade):
        """Verify selecting a job populates payload input."""
        # Setup job with payload
        job_with_payload = {
            "job_id": "test-job-1",
            "job_name": "Test Job",
            "cron": "*/5 * * * *",
            "target_mode": "agent",
            "enabled": True,
            "notification_target": {"telegram_session_uid": "desktop:test_session"},
            "payload": {"custom_key": "custom_value", "project_slug": "test-project"},
        }
        scheduler_widget._jobs_by_id["test-job-1"] = job_with_payload

        # Add job to list
        from PySide6.QtWidgets import QListWidgetItem
        from PySide6.QtCore import Qt
        item = QListWidgetItem("Test Job")
        item.setData(Qt.ItemDataRole.UserRole, "test-job-1")
        scheduler_widget.jobs_list.addItem(item)
        scheduler_widget.jobs_list.setCurrentItem(item)

        # Trigger selection
        scheduler_widget._on_job_selected()

        # Verify payload is populated
        payload_text = scheduler_widget.payload_input.toPlainText()
        assert payload_text
        parsed = json.loads(payload_text)
        assert parsed["custom_key"] == "custom_value"
        assert parsed["project_slug"] == "test-project"

    def test_save_job_with_payload(self, scheduler_widget, mock_facade):
        """Verify save_job passes payload to facade."""
        scheduler_widget.payload_input.setPlainText('{"test": "payload"}')
        scheduler_widget.project_selector.setCurrentIndex(0)
        scheduler_widget.session_selector.setCurrentIndex(0)
        scheduler_widget.mode_selector.setCurrentIndex(0)
        scheduler_widget.job_name_input.setText("Test Job")
        scheduler_widget.cron_input.setText("0 0 * * *")

        scheduler_widget._save_job()

        # Verify create_scheduler_job was called with payload
        mock_facade.create_scheduler_job.assert_called_once()
        call_kwargs = mock_facade.create_scheduler_job.call_args[1]
        assert call_kwargs["payload"] == {"test": "payload"}

    def test_update_job_with_payload(self, scheduler_widget, mock_facade):
        """Verify update_scheduler_job passes payload to facade."""
        # Setup existing job
        job_with_payload = {
            "job_id": "test-job-1",
            "job_name": "Test Job",
            "cron": "*/5 * * * *",
            "target_mode": "agent",
            "enabled": True,
            "notification_target": {"telegram_session_uid": "desktop:test_session"},
            "payload": {"original": "data"},
        }
        scheduler_widget._jobs_by_id["test-job-1"] = job_with_payload

        # Select job
        from PySide6.QtWidgets import QListWidgetItem
        from PySide6.QtCore import Qt
        item = QListWidgetItem("Test Job")
        item.setData(Qt.ItemDataRole.UserRole, "test-job-1")
        scheduler_widget.jobs_list.addItem(item)
        scheduler_widget.jobs_list.setCurrentItem(item)
        scheduler_widget._on_job_selected()

        # Modify payload
        scheduler_widget.payload_input.setPlainText('{"updated": "payload"}')

        scheduler_widget._save_job()

        # Verify update_scheduler_job was called with new payload
        mock_facade.update_scheduler_job.assert_called_once()
        call_kwargs = mock_facade.update_scheduler_job.call_args[1]
        assert call_kwargs["payload"] == {"updated": "payload"}

    def test_job_label_includes_payload_summary(self, scheduler_widget):
        """Verify job list label includes payload key summary."""
        job = {
            "job_id": "test-job-1",
            "job_name": "Test Job",
            "cron": "*/5 * * * *",
            "target_mode": "agent",
            "enabled": True,
            "payload": {"key1": "v1", "key2": "v2", "project_slug": "test"},
        }

        label = scheduler_widget._job_label(job)

        assert "payload: key1, key2" in label
        assert "project_slug" not in label  # Excluded from summary

    def test_job_label_truncates_payload_summary(self, scheduler_widget):
        """Verify job list label truncates payload summary to 3 keys."""
        job = {
            "job_id": "test-job-1",
            "job_name": "Test Job",
            "cron": "*/5 * * * *",
            "target_mode": "agent",
            "enabled": True,
            "payload": {"k1": "v1", "k2": "v2", "k3": "v3", "k4": "v4", "k5": "v5"},
        }

        label = scheduler_widget._job_label(job)

        assert "payload: k1, k2, k3..." in label

    def test_job_label_without_payload(self, scheduler_widget):
        """Verify job list label works without payload."""
        job = {
            "job_id": "test-job-1",
            "job_name": "Test Job",
            "cron": "*/5 * * * *",
            "target_mode": "agent",
            "enabled": True,
            "payload": {},
        }

        label = scheduler_widget._job_label(job)

        assert "payload:" not in label
