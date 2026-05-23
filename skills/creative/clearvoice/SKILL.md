---
name: clearvoice
description: Use when rewriting or reviewing prose for clarity, credibility, and natural voice without inventing facts. Good for emails, website copy, docs, proposals, explanations, and founder or product writing.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [writing, editing, credibility, prose, voice, copy]
    related_skills: [humanizer]
---

# ClearVoice

## Overview

ClearVoice edits text so it becomes clearer, more credible, and easier to read. It is not an AI-detector evasion trick. The goal is trust: plain language, concrete claims, natural rhythm, and no fake specificity.

This is a general-purpose skill for any Hermes user. It should not contain one user's brand voice, company positioning, tenant facts, or private preferences. Those belong in local memory, local overrides, tenant profiles, or the user's own voice samples.

ClearVoice and Humanizer overlap, but they optimize for different outcomes:
- Use ClearVoice for credibility, business trust, and factual restraint.
- Use Humanizer for stronger de-AI, anti-slop, or personality editing.

Core principle:

Rewrite for credibility, not disguise. If the original lacks evidence, do not add fake details. Prefer plain, concrete, slightly imperfect human language over polished persuasion.

## When to Use

Use this skill when the user asks to:
- make text clearer, sharper, more natural, or less AI-sounding
- improve website copy, emails, proposals, product text, documentation, notes, or public posts
- remove filler, hype, generic marketing language, vague claims, or chatbot residue
- make a draft sound like a specific person, if a voice sample or clear tone direction is provided
- explain a technical or complex topic in simpler language
- review writing for credibility problems before publishing

Do not use this skill for:
- fiction or intentionally stylized writing where factual restraint is not the main goal
- citation-heavy academic work unless sources are provided and checked
- translation-only tasks, unless the user also asks for style editing

## Hard Rules

1. Do not invent facts.
   - No invented names, dates, metrics, studies, quotes, customers, locations, features, certifications, prices, or sources.
   - If specificity is needed but not provided, either keep the statement general or mark that a source or detail is needed.

2. Preserve the meaning.
   - Keep numbers, product names, company names, legal claims, technical terms, dates, URLs, and calls to action unless the user asks to change them.
   - Do not strengthen weak claims without evidence.
   - Do not turn possibility into certainty.

3. Keep technical and legal text safe.
   - In technical docs, correctness beats style.
   - In legal or compliance-adjacent text, prefer minimal edits over expressive rewrites.

4. Remove AI-sounding residue only when it improves the text.
   - Watch for significance inflation, vague authority, rule-of-three filler, sycophancy, generic conclusions, heavy signposting, over-polished transitions, em dash overuse, bold-label bullets, and empty “at its core” framing.
   - These are warning signs, not absolute bans.

5. Match the requested language and audience.
   - If the user specifies a language, write in that language.
   - If the audience is non-technical, explain like a normal person would say it aloud.
   - If the user gives a voice sample, match that sample over generic style rules.
   - After a user, tenant, or agent profile is customized, treat that local voice profile as the authority and refine the rewrite against it.
   - As the agent learns more about the user's writing preferences, update the local user, tenant, or agent voice profile or local override. Do not bake those personal details into this shared skill.

## Plain Explanation Mode

Use this mode when the user asks for a concept explanation or says a text is too technical, too long, too abstract, too English-heavy, or too hard for a layperson.

Rules:
- Explain it like a normal person would say it aloud.
- Use fewer technical terms. Translate or replace jargon where possible.
- Keep only the concepts the reader needs right now.
- Prefer one simple analogy over a full architecture map.
- Shorten aggressively. If a section does not help the reader understand the point, remove it.
- Use bullet lists only when they clarify something. Do not use lists as the default shape.
- Clearly separate people, products, source code, templates, servers, and running instances. Do not collapse a named agent, service, or product with the infrastructure it runs on.

## Editing Modes

Choose the lightest mode that satisfies the task.

Minimal edit: use when the original mostly works or when factual, legal, or technical precision matters. Keep the structure and claims. Remove filler and awkward phrasing.

Full rewrite: use when the original is bloated, generic, or structurally weak. Reorganize if needed, but keep the factual boundary intact.

Critique only: use when the user asks for feedback rather than rewriting. Identify the biggest credibility and style issues, then give concrete fix direction.

Technical: use for docs, PRs, issues, architecture notes, and operational explanations. Preserve exact meaning. Remove flourish.

Business or website copy: use for public-facing or customer-facing text. Keep the benefit clear, the proof concrete, and the tone restrained.

Email: make the ask clear, keep the tone human but not chatty, and remove apology loops or excessive politeness.

## Default Output

Unless the user asks for a detailed audit, provide:

1. Final rewrite
2. Up to 3 short notes only if important

Do not default to long sections like draft, audit, final, and changes. That is too much for normal use.

If editing a file:
- read the file first
- make targeted patches where possible
- show what changed or summarize the changed section
- do not silently rewrite large files unless the user asked for it

## Credibility Checklist

Before finalizing, check:
- Did I invent any fact, metric, quote, source, or example?
- Did I preserve all names, numbers, terms, claims, and constraints?
- Did I accidentally make a cautious claim stronger?
- Is the text clearer, shorter, and more concrete?
- Does it fit the requested language and audience?
- Did I avoid generic AI polish and over-structured output?

## Common Patterns to Remove or Reduce

Treat these as signals, not hard bans:

- “serves as a testament”, “pivotal”, “landscape”, “unlock”, “seamless”, “robust”, “game-changing”
- “at its core”, “the real question is”, “what truly matters”
- “not just X, but Y” when it adds drama without clarity
- vague authorities: “experts say”, “industry observers”, “many believe”
- empty -ing tails: “highlighting”, “showcasing”, “underscoring”
- generic conclusions: “the future looks bright”, “exciting times ahead”
- chatbot residue: “I hope this helps”, “let me know if”, “great question”
- overformatted bullets with bold labels where prose would be clearer

## Pitfalls

1. Mistaking specificity for credibility.
   - Fake specifics are worse than generic prose.
   - If details are missing, write within the known facts.

2. Over-humanizing technical or business text.
   - Personality can damage trust when the audience expects precision.
   - Prefer clear and calm over colorful unless the user asks for a stronger voice.

3. Treating style tells as grammar rules.
   - Passive voice, em dashes, curly quotes, and hyphenated compounds are not automatically wrong.
   - Fix overuse or misuse, not normal language.

4. Rewriting away the user's intent.
   - Strong editing is not permission to change the claim.
   - Preserve the point unless the user asks for repositioning.

5. Producing a technically correct but unreadable explanation.
   - If the user asks for a layperson explanation, do not preserve the full architecture dump.
   - Reduce terms, avoid unnecessary English jargon, and explain only the useful shape.

## Verification Checklist

- [ ] No invented facts or sources
- [ ] Claims stayed within the provided evidence
- [ ] Numbers, names, terms, and calls to action preserved
- [ ] Tone matches language and audience
- [ ] Hype and filler removed
- [ ] Output is concise by default
