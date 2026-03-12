# Team: coding

- role: Software development and code review
- lead_pi: coder
- enabled: true
- always_on: false
- mode: default

## Pis

### coder
- model: claude-sonnet-4-5-20250929
- tools: coder
- tools_deny: none
- max_turns: 15

### reviewer
- model: claude-sonnet-4-5-20250929
- tools: code-reviewer
- tools_deny: none
- max_turns: 10
