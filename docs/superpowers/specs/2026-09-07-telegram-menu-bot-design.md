# Telegram Menu Bot Design

## Purpose

Create a Telegram bot constructor. The platform owner manages mirror bots from a private control panel. Each mirror bot has exactly one assigned owner, who manages that bot's profile, user-facing menu, sections, and buttons entirely inside Telegram.

## Technology

- Python 3.12 and aiogram 3.
- SQLite database stored at `data/bot.db`.
- Long polling for local development and the initial deployment. The application layout must remain suitable for later VPS deployment.
- The controller bot token and platform owner ID are held in `.env`: `BOT_TOKEN` and `ADMIN_ID`.
- Mirror bot tokens are stored in the protected SQLite database. They are never shown back in Telegram messages or logs.

## Access Control

- The platform owner, whose numeric ID equals `ADMIN_ID`, alone can use the controller bot's `/admin` panel.
- The platform owner adds a mirror by entering its BotFather token and then the numeric Telegram ID of its designated owner.
- The system verifies the token using `getMe` before saving it.
- In a mirror, only the designated owner can use `/admin` and administration actions; other users see only that mirror's public menu.
- Controller actions can list mirrors and start, stop, or delete each one.
- Stopping a mirror terminates its polling task. Its configuration remains in SQLite and it can later be restarted.
- Callback data must identify only database IDs and action codes; administrative handlers must check the relevant owner again on every callback.

## Public Experience

- `/start` in a mirror opens that mirror's configured root section, displays its text and inline keyboard.
- A button has an optional emoji prefix, a title, and a style: normal, blue (`primary`), green (`success`), or red (`danger`).
- A text button sends its configured text response.
- A URL button opens its validated `http` or `https` link.
- A submenu button opens the selected target section.
- Nested sections include a generated Back button that returns to the prior section.

## Administration Experience

- `/admin` in a mirror opens its owner-only dashboard.
- Profile settings allow changing the bot's Telegram name and description through the Bot API, plus the welcome text stored by the bot.
- Section management supports listing, creating, editing title and text, choosing the root section, changing order, and deleting a section.
- Button management within a section supports listing, creating, editing title, emoji, type, payload, style, and order, plus deletion.
- For a submenu button, the owner selects an existing target section.
- Deleting a referenced section is rejected until buttons targeting it are changed or removed.
- Multi-step edits use aiogram FSM states and have a visible Cancel action.

## Data Model

- `mirrors`: bot token, verified Telegram bot ID and username, assigned owner ID, and enabled status.
- `settings`: mirror ID, welcome text, root section ID.
- `sections`: mirror ID, title, body text, sort order.
- `buttons`: mirror ID, parent section ID, title, emoji, action type (`text`, `url`, `submenu`), action payload, style, sort order.

## Validation and Errors

- Validate Telegram limits for button labels, message text, callback payloads, profile name, and description before saving.
- Validate URL buttons with an `http` or `https` scheme.
- Require text payloads for text buttons and a valid target section for submenu buttons.
- Present concise, recoverable validation errors to the owner and preserve the current edit flow where practical.

## Operational Requirements

- Provide `.env.example`, dependency definition, setup instructions, and a local run command.
- Create the SQLite schema automatically on first startup and start all enabled mirrors.
- Include a VPS deployment guide for a later systemd-based deployment.
- Do not commit tokens, owner IDs, or database data.

## Verification

- Unit tests cover validation and data-access behavior.
- Handler-level tests cover platform-owner and mirror-owner authorization plus public rendering of every button type.
- A manual smoke-test checklist verifies controller `/admin`, mirror creation, start/stop/delete, mirror `/start`, mirror `/admin`, profile update, section management, and every button style/type.
