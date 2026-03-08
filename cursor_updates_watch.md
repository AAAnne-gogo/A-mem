# Cursor updates watch

This repository now includes `cursor_updates_watch.py`, a small watcher for Cursor release activity.

## What it checks

- Cursor changelog: `https://cursor.com/changelog/rss.xml`
- Cursor blog: `https://cursor.com/blog`
- Official X timeline mirror for `@cursor_ai`: `https://r.jina.ai/http://x.com/cursor_ai`

## Schedule behavior

- Default timezone: `Asia/Shanghai`
- Default run window: local `09:00`
- The script only performs one scheduled run per local date.
- `--force` runs immediately without consuming the scheduled slot and without updating seen-state.

## Output

Runtime output is written under `.cursor_updates/`:

- `.cursor_updates/latest.md`
- `.cursor_updates/reports/*.md`
- `.cursor_updates/state.json` after scheduled runs

That directory is git-ignored so reports and state do not pollute commits.

## Examples

```bash
python3 cursor_updates_watch.py
python3 cursor_updates_watch.py --force
python3 cursor_updates_watch.py --timezone Asia/Shanghai --schedule-hour 9
```
