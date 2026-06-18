---
name: Bug report
about: Something is broken with the iMessage adapter
title: "[bug] "
labels: bug
assignees: tylerdotai
---

## What happened

<!-- Plain description of the symptom. -->

## Steps to reproduce

1. ...
2. ...

## Expected behavior

<!-- What you expected to see. -->

## Actual behavior

<!-- What you actually saw. Paste gateway.log / imsg output if relevant. -->

```
<paste logs here>
```

## Environment

- macOS version: (Apple menu → About This Mac)
- Hermes version: (`hermes --version` or git SHA)
- imsg version: (`imsg --version`)
- Python binary running gateway: (`ps aux | grep gateway | head -1`)
- Adapter commit SHA: (`git rev-parse HEAD` inside this repo)

## Notes

<!-- Anything else — FDA status, config.yaml `imsg:` block, .env, etc. -->
