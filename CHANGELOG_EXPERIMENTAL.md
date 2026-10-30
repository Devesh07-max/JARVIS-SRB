## Requested fixes — 2026-08-27

- Replaced the two task-reset buttons in `/customisations` with **Reset Customisation**.
- Reset Customisation clears caption, rename tag, replacement rules, remove-word rules, and forward Chat ID while preserving thumbnails, login, and Premium.
- Forward Chat ID validation no longer rejects a destination solely because Telegram returned stale/incomplete administrator metadata; forwarding now uses copy → forward → direct-media fallback.
- Busy-task prompts now point users to `/reset`.
- Added a per-file batch timeout (`FILE_TASK_TIMEOUT`, default 1800 seconds) so a hung/corrupt item is skipped and the batch continues.

# Experimental project changes

This build keeps the existing project structure and adds:

- Plan-aware access control: Basic, Standard, Premium, Ultimate and Elite.
- `/batch` restricted to Standard/Premium/Ultimate/Elite.
- Daily plan quotas with automatic daily reset.
- Admin plan assignment using `USER_ID PLAN DURATION`.
- Admin plan guide in `/admincommands`.
- Interactive `/buypremium` plan selection.
- Per-plan payment screen with QR-path support and Admin Contact button.
- `/cancel` and task reset clear transient processing/input state without deleting saved login/customisation settings.
- Single forwarding destination per user; legacy multiple IDs are reduced to the first configured ID.
- Automatic forwarding of the bot's own successfully uploaded message to that configured destination, with visible forwarding errors.
- Improved force-subscribe error handling for missing/invalid membership records.

## Payment QR

Put the QR image in the project root as `payment_qr.jpg`, or set `PAYMENT_QR_PATH` to another local file path. The payment UI is already wired to show the QR when the file exists.

## Plan prices

Edit the existing `PREMIUM_PLAN_*` values in `config.py`. The plan UI reads those values automatically.

## Important

Use the Telegram message/file workflow only for content you own or are authorized to process. Telegram content-protection restrictions remain enforced by Telegram and are not bypassed by this build.
