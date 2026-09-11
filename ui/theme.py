"""Visual layer: a small CSS block and HTML badge helpers. Presentation only."""
from __future__ import annotations

import html

import streamlit as st

CSS = """
<style>
:root { --ink:#0F172A; --muted:#64748B; --line:#E6E8EE; --card:#FFFFFF; --brand:#2563EB; }
[data-testid="stDecoration"], #MainMenu { display:none; }
.block-container { padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1240px; }
h1, h2, h3 { letter-spacing: -0.01em; }
.rfq-hero h1 { font-size: 2.35rem; margin-bottom: .25rem; color: var(--ink); }
.rfq-hero p  { font-size: 1.1rem; color: var(--muted); margin-top: 0; }
.rfq-kicker { font-size: .72rem; letter-spacing:.12em; text-transform: uppercase; color: var(--muted); font-weight: 600; }
.rfq-title { font-size: 1.45rem; font-weight: 700; color: var(--ink); margin: 0 0 .15rem 0; }
.rfq-sub { color: var(--muted); font-size: .95rem; }
.badge { display:inline-block; padding: .12rem .55rem; border-radius: 999px; font-size: .72rem; font-weight: 600; letter-spacing: .02em; vertical-align: middle; }
.badge.required { background:#FEE2E2; color:#991B1B; }
.badge.recommended { background:#FEF3C7; color:#92400E; }
.badge.optional { background:#F1F5F9; color:#475569; }
.badge.buyer { background:#DCFCE7; color:#166534; }
.badge.edited { background:#DCFCE7; color:#166534; }
.badge.ai { background:#EDE9FE; color:#5B21B6; }
.badge.missing { background:#FEE2E2; color:#991B1B; }
.badge.unknown { background:#E0F2FE; color:#075985; }
.badge.na { background:#F1F5F9; color:#64748B; }
.badge.conflict { background:#FFEDD5; color:#9A3412; }
.badge.status-ready { background:#DCFCE7; color:#166534; }
.badge.status-not { background:#FEF3C7; color:#92400E; }
.badge.status-sent { background:#DBEAFE; color:#1E40AF; }
.rfq-q { font-weight: 600; font-size: 1.0rem; color: var(--ink); margin-bottom: .1rem; }
.rfq-why { color: var(--muted); font-size: .86rem; margin-bottom: .35rem; }
.rfq-score { font-size: 2.6rem; font-weight: 700; line-height: 1; color: var(--ink); }
.rfq-score small { font-size: 1rem; color: var(--muted); font-weight: 500; margin-left: .25rem; }
.rfq-check { font-size: .9rem; line-height: 1.5; margin: .15rem 0 0 0; padding: 0; list-style: none; }
.rfq-check li { padding: .16rem 0 .16rem 1.35rem; text-indent: -1.35rem; }
.rfq-check .ic { display: inline-block; width: 1.35rem; text-indent: 0; font-weight: 700; }
.rfq-check .lbl { color: var(--ink); }
.rfq-check .val { color: var(--muted); }
.ic.ok { color:#16A34A; } .ic.ai { color:#7C3AED; } .ic.miss { color:#DC2626; } .ic.na { color:#94A3B8; } .ic.conf { color:#EA580C; } .ic.unk { color:#0284C7; }
.rfq-field-label { font-size: .78rem; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }
.rfq-field-value { font-size: 1rem; color: var(--ink); }
.rfq-field-value.dim { color: var(--muted); font-style: italic; }
div[data-testid="stExpander"] details { border-radius: 10px; }
.stChatMessage { padding: .4rem .6rem; }
.rfq-section-head { font-size: .8rem; font-weight: 700; letter-spacing: .04em; text-transform: uppercase;
  color: var(--muted); margin: 1rem 0 .35rem 0; padding-bottom: .2rem; border-bottom: 1px solid var(--line); }
.rfq-line-id { font-size: .72rem; font-weight: 700; color: var(--muted); padding-top: 1.9rem; letter-spacing: .04em; }
.rfq-line-sep { border-bottom: 1px solid var(--line); margin: .5rem 0 .2rem 0; }
.rfq-line { padding: .45rem 0; border-bottom: 1px solid var(--line); font-size: .92rem; }
.rfq-muted, .rfq-muted * { color: #94A3B8 !important; }
.rfq-muted div[data-testid="stExpander"] details { border-color: var(--line); background: transparent; }
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def badge(kind: str, text: str) -> str:
    return '<span class="badge %s">%s</span>' % (kind, html.escape(text))


def esc(text: str) -> str:
    return html.escape(str(text if text is not None else ""))
