# Cursor 每日更新

- 生成时间: 2026-03-07 18:09:25 CST
- 运行模式: 强制校验
- 来源: changelog / blog / @cursor_ai

## Changelog
1. [Automations](https://cursor.com/changelog/03-05-26)
   - 发布时间: Mar 5, 2026
   - 摘要: Cursor now supports [automations](https://cursor.com/docs/cloud-agent/automations) for building always-on agents that run based on triggers and instructions you define. Automations run on schedules or are triggered by events from Slack, Linear, GitHub, PagerDuty, and webhooks.
2. [Cursor in JetBrains IDEs](https://cursor.com/changelog/03-04-26)
   - 发布时间: Mar 4, 2026
   - 摘要: Cursor is now available in IntelliJ IDEA, PyCharm, WebStorm, and other JetBrains IDEs through the Agent Client Protocol (ACP). With Cursor ACP, developers who rely on JetBrains for Java and multilanguage support can use any frontier model from OpenAI, Anthropic, Google, and Cursor for agent-driven development.
3. [MCP Apps and Team Marketplaces for Plugins](https://cursor.com/changelog/2-6)
   - 发布时间: Mar 3, 2026
   - 摘要: This release introduces interactive UIs in agent chats, a way for teams to share private plugins, and improvements to core capabilities like Debug mode. [MCP Apps](https://cursor.com/docs/context/mcp#mcp-apps) support interactive user interfaces like charts from [Amplitude](https://cursor.com/marketplace/amplitude), diagrams from [Figma](https://cursor.com/marketplace/figma), and whiteboards from [tldraw](https://...
4. [Bugbot Autofix](https://cursor.com/changelog/02-26-26)
   - 发布时间: Feb 26, 2026
   - 摘要: Bugbot can automatically fix issues it finds in pull requests. [Autofix](https://cursor.com/docs/bugbot#autofix) runs cloud agents on their own machines to test changes and propose fixes directly on your PR. Today, over 35% of Bugbot Autofix changes are merged into the base PR.
5. [Cloud Agents with Computer Use](https://cursor.com/changelog/02-24-26)
   - 发布时间: Feb 24, 2026
   - 摘要: Cloud agents can now use the software they create to test changes and demo their work. After onboarding onto your codebase, each agent runs in its own isolated VM with a full development environment. Cloud agents produce merge-ready PRs with artifacts (videos, screenshots, and logs) that make it possible to quickly review their changes.

## Blog
1. [Build agents that run automatically](https://cursor.com/blog/automations)
   - 发布时间: 2026-03-05T12:00:00.000Z
   - 摘要: We're introducing Cursor Automations to build always-on agents. These agents run on schedules or are triggered by events like a sent Slack message, a newly created Linear issue, a merged GitHub PR, or a PagerDuty incident. In addition to these built-in integrations, you can configure your own custom events with webhooks.
2. [Cursor is now available in JetBrains IDEs](https://cursor.com/blog/jetbrains-acp)
   - 发布时间: 2026-03-04T12:00:00.000Z
   - 摘要: Cursor is now available in IntelliJ IDEA, PyCharm, WebStorm, and other JetBrains IDEs through the [Agent Client Protocol](https://agentclientprotocol.com/) (ACP). Developers who rely on IntelliJ IDEA and other [JetBrains IDEs](https://www.jetbrains.com/ides/) for strong Java and multilanguage support can now use any frontier model with Cursor for agent-driven development.
3. [How technical support at Cursor uses Cursor](https://cursor.com/blog/cursor-support)
   - 发布时间: 2026-03-03T12:00:00.000Z
   - 摘要: Support investigations are fundamentally research problems, which is why the slowest part of responding to customer challenges has always been gathering the right context. By collapsing code, logs, team knowledge, and past conversations into a single Cursor session, we've removed that bottleneck for most of our work. Today, over 75% of Cursor's support interactions run through Cursor itself, increasing support eng...
4. [PlanetScale protects production reliability with Bugbot](https://cursor.com/blog/planetscale)
   - 发布时间: 2026-03-02T12:00:00.000Z
   - 摘要: PlanetScale manages cloud database workloads for its customers' most sensitive data. Reliability is the product and every code change pushed to production must be flawless. As agents made code generation cheap and fast, code review became the new bottleneck in the software development lifecycle. To ensure correctness and ship code to production with confidence, PlanetScale adopted [Bugbot](https://cursor.com/bugbo...
5. [The third era of AI software development](https://cursor.com/blog/third-era)
   - 发布时间: 2026-02-26T19:35:24.000Z
   - 摘要: When we started building Cursor a few years ago, most code was written one keystroke at a time. Tab autocomplete changed that and opened the first era of AI-assisted coding. Then agents arrived, and developers shifted to directing agents through synchronous prompt-and-response loops. That was the second era. Now a third era is arriving. It is defined by agents that can tackle larger tasks independently, over longe...

## 官方 X (@cursor_ai)
1. We're introducing Cursor Automations to build always-on agents.
2. GPT 5.4 is now available in Cursor! We've found it to be more natural and assertive than previous models. It's currently the leader on our internal benchmarks.
3. Cursor can now continuously monitor and improve your codebase. Automations run based on triggers and instructions you define.
4. Cursor is now available in JetBrains IDEs through the Agent Client Protocol. We believe Cursor discovered a novel solution to Problem Six of the First Proof challenge, a set of math research problems that approximate the work of Stanford, MIT, Berkeley academics. Cursor's solution yields stronger results than the of...
5. Cursor now supports MCP Apps. Agents can render interactive UIs in your conversations.
6. Create and share private plugins with team marketplaces.
7. See everything new in Cursor:
8. Cursor can now automatically fix issues it finds in PRs with Bugbot Autofix.
