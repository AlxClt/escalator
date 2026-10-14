# escalator (Work in progress)

Cost-aware escalation routing for a data-analysis agent.

This repo builds a text to SQL agent that is evaluated on a task set, with several models acting as its brain: small local models and paid online models.

The task the agent is evaluated on are drawn from **ReViSQL** — Zhu, Jin, Choi, Kang, arXiv:2603.20004, 2026. We are usin their corrected version of Mini-BIRD-Dev, i.e. Arcwise-Plat-Full

The goal is to study an escalation policy over different early error signals (tool call error, repeated indentical queries tested...), and compute the cost vs accuracy curve for different threshold of these error signals.
