# API Spec: `agent/plugins/base.py`

Generated: 2026-06-17T10:46:18Z

## Classes
### `class ToolPlugin(ABC)` (line 13)
- `def get_plugin_id()` (line 17)
- `def get_function_prefix()` (line 20)
  - *Optional function name prefix for ToolRegistry.*
- `def initialize(config, services)` (line 31)
- `def close()` (line 35)
- `def get_source_name()` (line 38)
- `def get_message_handlers()` (line 46)
- `def get_inline_handlers()` (line 49)
- `def get_menu_label()` (line 52)
  - *Human-friendly name for the two-level plugin menu.*
- `def get_menu_actions()` (line 61)
  - *Actions shown as buttons in the plugin submenu.*
- `def awaiting_input(chat_id)` (line 73)
  - *Return True if this plugin is waiting for free-text input from the user.*
- `def cancel_input(chat_id)` (line 82)
  - *Cancel a pending input dialog for the given chat.*
- `def get_spec()` (line 91)
- `async def execute(args, ctx)` (line 95)

### `class DialogState` (line 109)
*State of an active dialog for a single chat.*

### `class DialogMixin` (line 117)
*Mixin that provides a standard multi-step dialog protocol.*
- `def start_dialog(chat_id, step, data, user_id)` (line 158)
- `def end_dialog(chat_id)` (line 166)
- `def get_dialog(chat_id)` (line 169)
- `def set_step(chat_id, step, data)` (line 178)
- `def is_cancel_text(cls, text)` (line 233)
- `def cancel_markup()` (line 245)
  - *Return an InlineKeyboardMarkup with a single 'Отмена' button.*
- `def dialog_button(label, data)` (line 258)
  - *Create a button whose callback_data is scoped to this plugin's dialog.*
- `def action_button(label, action, payload)` (line 279)
  - *Create a button for an autonomous callback handler (outside dialog).*
- `def parse_callback_payload(update)` (line 295)
  - *Extract the payload portion from a dialog/action button callback_data.*
- `def awaiting_input(chat_id)` (line 318)
- `def cancel_input(chat_id)` (line 321)
- `def dialog_steps()` (line 331)
  - *Return a mapping of step name -> handler.*
- `def callback_handlers()` (line 355)
  - *Return a mapping of action -> async handler for autonomous buttons.*
- `def step_hint(step)` (line 374)
  - *Optional hint shown when the user sends an unexpected content type.*
- `async def handle_message(update, context)` (line 405)
  - *Unified entry point called by the bot for every text message*
- `async def handle_callback(update, context)` (line 470)
  - *Unified entry point for dialog step callback buttons.*
- `def get_inline_handlers()` (line 588)
- `def get_message_handlers()` (line 614)
  - *Default implementation: returns a dict-config list for bot.py*
- `def extra_message_filters()` (line 635)
  - *Override to add extra content-type filters (e.g. PHOTO).*
