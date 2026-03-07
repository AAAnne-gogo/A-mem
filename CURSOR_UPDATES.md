# Cursor Updates Automation

This repository now includes a small automation helper for collecting Cursor product updates from:

- Cursor changelog: `https://cursor.com/changelog`
- Cursor blog: `https://cursor.com/blog`
- Official X account: `https://x.com/cursor_ai`

## How it works

The script is designed for an hourly cron trigger. It only writes a report when the local time reaches the configured hour, and it skips duplicate runs on the same local date.

Default behavior:

- Timezone: `Asia/Shanghai`
- Scheduled hour: `09:00`
- Report output: `reports/cursor-updates/YYYY-MM-DD.md`
- State file: `.cursor_updates_state.json`

## Commands

Run only when the current local time is 9 AM:

```bash
python3 cursor_updates.py
```

Force a run immediately:

```bash
python3 cursor_updates.py --force
```

Override timezone or hour:

```bash
python3 cursor_updates.py --timezone Asia/Shanghai --hour 9
```

## Notes

- Changelog and blog items are parsed directly from the official Cursor site.
- Official X posts are collected from a readable snapshot of the official profile page because public RSS endpoints are unreliable.
- The state file keeps track of seen items so the daily report can highlight what is new since the previous successful run.
