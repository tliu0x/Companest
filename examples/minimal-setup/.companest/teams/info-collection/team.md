# Team: info-collection

- role: Data collection and information gathering from external sources
- lead_pi: collector
- enabled: true
- always_on: true
- mode: default

## Pis

### collector
- model: deepseek-chat
- tools: collector
- max_turns: 5
