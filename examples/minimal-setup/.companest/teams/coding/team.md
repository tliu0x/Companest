# Team: coding

- role: Software development and code review
- lead_pi: coder
- enabled: true
- always_on: false
- mode: default

## Pis

### coder
- model: deepseek-chat
- tools: coder
- tools_deny: none
- max_turns: 15

### reviewer
- model: deepseek-chat
- tools: code-reviewer
- tools_deny: none
- max_turns: 10
