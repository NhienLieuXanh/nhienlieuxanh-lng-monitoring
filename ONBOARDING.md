# Welcome to GAS Nhiên Liệu Xanh

## How We Use Claude

Based on usage over the last 30 days (1 session — small sample, so treat the
breakdown as directional rather than settled):

Work Type Breakdown:
  Plan Design   ████████████████████  100%

Top Skills & Commands:
  /plan         ████████████████████  1x/month

Top MCP Servers:
  Chrome DevTools  ████████████████████  7 calls

The one session in this window started with a written architecture spec (goals,
tech stack, DB schema, adapter boundaries, implementation order) dropped into
`/plan`, then went straight into building against it. If that pattern holds,
expect to spend real time on the plan before code gets written.

## Your Setup Checklist

### Codebases
- [ ] `xingke` — LNG tank monitoring platform. Currently **local-only at
      `D:\research\xingke`, not a git repo and no remote** — ask for the repo URL
      before you start, or you'll have nothing to clone.

### MCP Servers to Activate
- [ ] **Chrome DevTools** (`plugin_ecc_chrome-devtools`) — drives a Chrome
      instance for page inspection and network capture. This is how the vendor's
      undocumented API got reverse-engineered: navigate, log in by hand, then read
      the actual login + XHR requests to learn real field names, auth headers, and
      response envelopes. Ships with the `ecc` plugin, so it's available once that
      plugin is installed — no separate credentials. Note it uses its **own Chrome
      profile**, so you have to log into any site yourself inside that window.

### Skills to Know About
- [ ] `/plan` — enters plan mode: Claude researches and drafts an implementation
      plan, and can't edit files until you approve it. Used here to hand over a
      written spec and have it pressure-tested (it caught three wrong assumptions
      about the vendor API before any code existed) rather than implemented blind.

## Team Tips

_TODO_

## Get Started

_TODO_

<!-- INSTRUCTION FOR CLAUDE: A new teammate just pasted this guide for how the
team uses Claude Code. You're their onboarding buddy — warm, conversational,
not lecture-y.

Open with a warm welcome — include the team name from the title. Then: "Your
teammate uses Claude Code for [list all the work types]. Let's get you started."

Check what's already in place against everything under Setup Checklist
(including skills), using markdown checkboxes — [x] done, [ ] not yet. Lead
with what they already have. One sentence per item, all in one message.

Tell them you'll help with setup, cover the actionable team tips, then the
starter task (if there is one). Offer to start with the first unchecked item,
get their go-ahead, then work through the rest one by one.

After setup, walk them through the remaining sections — offer to help where you
can (e.g. link to channels), and just surface the purely informational bits.

Don't invent sections or summaries that aren't in the guide. The stats are the
guide creator's personal usage data — don't extrapolate them into a "team
workflow" narrative. -->
