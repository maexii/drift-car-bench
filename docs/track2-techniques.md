# Track 2 Agent — Verbesserungstechniken (Überblick)

Zusammenfassung aller Changes/Techniken, mit denen der Track-2-Cerebras-Agent
verbessert wurde. Alle Zeilennummern beziehen sich auf
`src/track_2_agent_under_test_cerebras/car_bench_agent.py`.
Architektur-Details siehe [`architecture-sequential-calls.md`](architecture-sequential-calls.md).

## Überblick: Von 1 auf ≤5 sequentielle LLM-Calls pro Schritt

Kern der Verbesserung ist eine **3-stufige Pipeline** in `execute()` (statt eines
einzelnen LLM-Calls), hart gedeckelt auf 5 sequentielle Calls/Schritt — plus
deterministische Guards, die *ohne* zusätzliche Calls greifen.

```
execute() → ① _call_model_with_retries → ② _verify_and_maybe_revise → ③ _completeness_and_maybe_revise → ④ _decline_review_and_maybe_revise
            (Generierung + Retry-Enforcement)  (LLM-Reviewer State-Change)   (optional, im Submit AUS)      (Decline-Judge, optional, im Submit AUS)
```

② und ④ sind **gegenseitig exklusiv pro Step**: ② feuert nur bei State-Changing-
`tool_calls`, ④ nur bei `action=respond`. Der Worst Case bleibt daher 5 Calls
(① 3 + ein Reviewer 1 + dessen Revision 1).

---

## 1 · Orchestrierung & Compliance

| Technik | Was / Warum | Code | Snippet |
|---|---|---|---|
| **Hartes Call-Budget** | ≤5 sequentielle Calls je Baseline-Step (README-Constraint); Budget wird pro A2A-Runde zurückgesetzt | `execute()` :149 | `call_budget = int(os.getenv("TRACK2_MAX_CALLS_PER_STEP","5"))` |
| **Budget-Weitergabe & Guards** | Jede Stufe bekommt `max_calls`/`call_budget`; Verifier läuft nur, wenn ≥2 Calls übrig | :419 | `if call_budget - internal_calls < 2: skip` |
| **Audit-Log pro Step** | Nachweisbarkeit der Compliance im Log | :176 | `"Baseline step complete", sequential_llm_calls_this_step=...` |

## 2 · Strukturierte Ausgabe + „Denk-vor-Handeln"-Scaffold

| Technik | Was / Warum | Code | Snippet |
|---|---|---|---|
| **JSON-Schema-Output** | Erzwingt valide `{checks, action, content, tool_calls}` via Cerebras `response_schema` | `NEXT_ACTION_OUTPUT_SCHEMA` :1374 | `response_schema=NEXT_ACTION_OUTPUT_SCHEMA` |
| **`checks`-Objekt vor Aktion** | Modell muss erst `missing_capability`, `unspecified_value_source`, `scope_ok` ausfüllen → Selbst-Check als CoT | :1378 | `"Fill these checks BEFORE choosing the action."` |
| **Response-Scaffold (Flag, AUS)** | Bei „unknown"/Deflection strengeres Schema | :245 `TRACK2_RESPONSE_SCAFFOLD=0` | `response_schema = SCAFFOLD_OUTPUT_SCHEMA` |

## 3 · Stufe ① — Deterministische Enforcement-Retries

Statt dem Modell zu vertrauen, prüfen Regeln die Antwort und lösen bei Verstoß
einen **Retry mit Korrektur-Prompt** aus (`raise MalformedModelResponseError`) —
aber nie im letzten Versuch (inkonsistente Aktion > Fehler).

| Guard | Domänen-Regel | Code | Kernlogik |
|---|---|---|---|
| `checks_inconsistency()` | Selbstreport vs. Aktion konsistent? (z.B. `missing_capability` gesetzt, aber trotzdem State-Change) | :1278 | reject wenn `missing ∉ (none)` & state-changing |
| `phase_separation_error()` | „Information gathering vor Execution" (CAR-bench Paper) — kein State-Change ohne vorherigen Read; keine voreilige Rückfrage | :1212 | `state_changing and not _conversation_has_read_only_call()` |
| `navigation_editing_error()` | `SetNewNavigation_001`: bei aktiver Navigation Edit-Tools statt `set_new_navigation`; sonst erst `get_current_navigation_state` | :1179 | `_latest_navigation_active(messages) is True/None` |

```python
# _call_model_with_retries :314 — Enforcement nur solange Retries übrig
if attempt < attempts - 1 and os.getenv("TRACK2_ENFORCE_CHECKS","1") not in (...):
    if (inconsistency := checks_inconsistency(parsed, messages)) is not None:
        raise MalformedModelResponseError(inconsistency)   # → Korrektur-Prompt, neuer Versuch
```

## 4 · Kontext-abhängige Prompt-Injektionen (`build_next_action_prompt` :830)

Deterministisch erkannte Situationen werden als gezielte Hinweise in den Prompt
injiziert — das Modell sieht z.B. *nicht*, welche Tools fehlen, der Harness schon.

| Injektion | Trigger-Funktion | Zweck |
|---|---|---|
| `unavailable_capabilities_notice` | `absent_capabilities()` :1098 — Diff gegen `tool_catalog.json` | Transparenz statt Fake bei fehlenden Tools/Parametern |
| `toll_disclosure_notice` | `_recent_routes_include_toll()` :940 | Maut-Offenlegungspflicht bei Routen (`includes_toll:true`) |
| `user_cannot_provide_notice` | `_latest_user_message_deflects()` :907 (Regex) | Nicht erneut nach etwas fragen, das User nicht liefern kann |
| `unavailable_information_notice` | `_recent_tool_result_has_unknown()` :961 | Bei `"unknown"`-Feld nicht raten/nachfragen |

```python
absent = absent_capabilities(tools)          # canonical catalog − provided tools
if absent is not None:
    prompt["unavailable_capabilities_notice"] = {"detail": absent, "instruction": "... say transparently ... do not fake ..."}
```

## 5 · Stufe ② — LLM-Verifier (Self-Critique)

| Technik | Was / Warum | Code |
|---|---|---|
| **Zweiter Reviewer-Call** | Kompakter, strenger Reviewer prüft *nur* State-Changing-Aktionen; `verdict ∈ {approve, revise}`; max. 1 Revision; bricht Turn nie ab | `_verify_and_maybe_revise()` :395 |
| **Risk-Gate (Budget-/Latenz-Spar)** | Überspringt Review im „langweilig-korrekten" Fall: 1 State-Change + alle Argumente nachvollziehbar | :426 |
| **Günstiger Call** | `reasoning_effort="low"`, `max_completion_tokens=512`, eigenes `VERIFIER_OUTPUT_SCHEMA` | :457 |
| **Reviewer-Checkliste** | Scope / Value-Provenance / Readiness | `build_verifier_prompt()` :1578 |

```python
# Risk-Gate :430 — spart den Verifier-Call, wenn Aktion offensichtlich sauber ist
if not risk_context and len(state_changing) <= 1 \
   and _argument_values_traceable(action.get("tool_calls"), messages):
    return inference_result        # kein 2. Call
```

## 5b · Stufe ④ — Reasoning-freier Decline-Judge (Flag, AUS)

| Technik | Was / Warum | Code |
|---|---|---|
| **Schmaler Decline-Judge** | Zielt auf den größten hall-Fail (`r_user_end_conversation`): ein echtes Capability-Limit wird als *Info-Mangel* geframt / der Decline in die Länge gezogen. Feuert **nur** bei `action=respond` **und** deterministisch belegter Limitierung (`absent_capabilities()` ≠ None **oder** `_recent_tool_result_has_unknown()`) | `_decline_review_and_maybe_revise()` :639 `TRACK2_DECLINE_JUDGE=0` |
| **Reasoning-frei** | Der Judge sieht *Fakten* (User-Anfrage, fehlende Tools/Params, Unknown-Flag) + den fertigen Antworttext — **nicht** das `checks`/Reasoning des Generators (`_messages_for_prompt` speichert es nicht) → echte Unabhängigkeit statt „stimmt sich selbst zu" | `build_decline_judge_prompt()` |
| **Approve-Bias, 1 Revision** | Nur `revise` bei realem Problem; Checkliste kodiert das PASS-Muster (einmal entschieden ablehnen) + die `missing_tool_parameter`/`missing_tool_response`-Nuance (Teil tun, nur fehlendes Stück benennen, nicht pauschal) | `DECLINE_JUDGE_INSTRUCTIONS` |

```python
# _decline_review_and_maybe_revise :639 — Trigger-Gate (respond + belegte Limitierung)
if action.get("action") != "respond": return inference_result
absent = absent_capabilities(tools)
has_unknown = _recent_tool_result_has_unknown(messages)
if absent is None and not has_unknown: return inference_result   # kein Call im Normalfall
```

## 6 · Anti-Halluzination bei Argumenten

| Technik | Was / Warum | Code |
|---|---|---|
| `_argument_values_traceable()` | Jeder State-Change-Argumentwert muss **wörtlich** im Gespräch vorkommen (User-Worte, Tool-Results) — kein Erfinden/Defaulten | :1241 |
| `_find_placeholder_argument()` | Verwirft Platzhalter-Argumente (`<...>`, `{{...}}`, `to_be_filled`, `tbd`) | :1328 |

## 7 · System-Prompt-Engineering

| Baustein | Inhalt | Code |
|---|---|---|
| `CEREBRAS_DEVELOPER_INSTRUCTIONS` | Reasoning-Layer-Prompt: Transparenz bei fehlenden Capabilities, „ask don't guess" für user-eigene Werte, `get_user_preferences`-Ausnahme, Disambiguierung | :1606 |
| `CANONICAL_WORKFLOWS` (Flag, AUS) | Aus gelösten Tasks „gemined" (z.B. Klima→`get_climate_settings` first, E-Mail-Kette) | :1506 `TRACK2_WORKFLOWS=0` |
| `VERIFIER_INSTRUCTIONS` / `COMPLETENESS_INSTRUCTIONS` | Strenge, high-precision Reviewer-Rollen | :1533 / :1555 |

## 8 · Stufe ③ + Infrastruktur

| Technik | Was / Warum | Code |
|---|---|---|
| `_completeness_and_maybe_revise()` | Fängt „Abschluss trotz offener expliziter User-Anfrage" ab (im Submit **AUS**, teilt Budget-Guard) | :531 |
| **Direktes Cerebras-SDK** | Ablösung von LiTELLM; strukturierte Outputs, Rate-Limit-Header, Quota-Wait-Retry | `_call_model_with_retries` :232 |
| **Turn-Metrics** | Token/Cost/Calls-Aggregation über A2A-Runden (≠ per-Step-Budget!) | `_record_turn_metrics()` :739 |
| **Feature-Flags** | Alles per `TRACK2_*`-Env toggelbar → A/B-Experimente ohne Codeänderung | durchgängig |

---

## Worst-Case-Budget (aus `architecture-sequential-calls.md`)

| Stufe | Max Calls | Laufsumme |
|---|---|---|
| ① Executor (1 + bis 2 Retries) | 3 | 3 |
| ② Verifier | 1 | 4 |
| ③ Revision | 1 | **5** |

**Maximum = 5**, Normalfall = 3 (Executor 1 + Verifier 1 + Revision 1); reine
Read-only-Turns enden nach ① (1–3). Ø ~105k Tokens/Task (Limit 500k).

## Flag-Defaults im eingereichten Config

- **AN:** `MAX_CALLS_PER_STEP=5`, `CATALOG_NOTICE`, `TOLL_NOTICE`, `ENFORCE_CHECKS`,
  `PHASE_SEPARATION`, `NAV_EDITING`, `VERIFIER`, `VERIFIER_GATE`
  (`VERIFIER_REASONING_EFFORT=low`)
- **AUS:** `RESPONSE_SCAFFOLD`, `WORKFLOWS`, `COMPLETENESS`, `DECLINE_JUDGE`
  (`DECLINE_JUDGE_REASONING_EFFORT=low` wenn an)
