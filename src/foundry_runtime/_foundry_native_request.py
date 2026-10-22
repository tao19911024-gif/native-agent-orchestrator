"""Project a graph-bound role into one repository-aware native request."""
from __future__ import annotations

import json

from ._foundry_response_contract import CollaborationError, response_contract_for_request
from ._execution_plan_prompt import WAIT_GUIDANCE, EVIDENCE_GUIDANCE


# Compatibility name for existing receipts. It is no longer a source-packet budget.
RENDERED_REQUEST_BYTES = 4 * 1024 * 1024


def build_prompt(request, backend, native_error, *, max_input_bytes=RENDERED_REQUEST_BYTES):
    if not isinstance(request, dict):
        raise native_error("native request must be an object")
    try:
        contract = response_contract_for_request(request)
        if "response_contract" in request and request["response_contract"] != contract:
            raise native_error("request changed the authoritative response contract")
        kind = request.get("kind")
        if backend not in {"codex", "claude"}:
            raise native_error("unknown analyst backend")
        roles = {
            "coordinate": (
                "You coordinate this development task. Delegate edits and tests to the existing selected worker; "
                "do not perform those edits yourself. Inspect relevant sources and returned evidence as needed. "
                "Return delegate with the next concrete instruction when work remains, or review to send the "
                "candidate to independent reviewers. Review is a routing decision, not an acceptance verdict. "
                "Keep instructions focused on the goal, constraints and acceptance; let the worker choose tools. "
                "Reuse the conversation's decisions and the worker's valid evidence. If operator input is needed, "
                "delegate that question to the worker's existing input channel."),
            "plan": (
                "Understand the goal and acceptance criteria. Inspect the repository with your tools, "
                "then return either a ready decision or concrete investigation tasks."),
            "analyse": (
                "Investigate the assigned question in the repository. Use the tools and files you judge "
                "useful, and report what the repository establishes."),
            "synthesize": (
                "Synthesize the completed investigations into the requested decision. Preserve uncertainty "
                "when the repository does not settle a fact."),
            "solution": (
                "Inspect the repository and propose a coherent implementation that meets the goal and "
                "acceptance. The executor remains free to revise the process while doing the work."),
            "review": (
                "Independently judge the candidate against the order's acceptance criteria. Inspect the "
                "target repository and run any focused checks you need. Form your own verdict from the "
                "candidate and its evidence, not another reviewer's conclusion. Do not inspect same-round "
                "reviewer messages, logs, receipts or verdicts, including your own in-progress review artifacts. "
                "Prior findings explicitly supplied for a correction remain valid review inputs. "
                "Reuse applicable recorded test results; run a focused probe for a concrete uncertainty, "
                "and broaden testing only when the change or evidence warrants it. "
                "Keep lengthy command output in a file and read the relevant portions. Echo candidate_commit and "
                "packet_sha256 exactly. Return BLOCK only for a concrete P0/P1 defect; otherwise PASS, "
                "recording any P2/P3 observations."),
        }
        instruction = roles.get(kind, "Complete the requested repository task and return the required result.")
        if kind == 'coordinate' and request.get('effective_coordination') == 1:
            instruction += (
                ' Decision packet: events contains only new entries not already carried in '
                'unanswered_live_events; address that durable list too. Start from the last '
                'decision/progress note in this conversation or manager_handoff, then compare '
                'the supplied new evidence, operator input and candidate/source revisions. '
                'Keep a compact decision brief in instruction: settled decisions with reasons '
                'and evidence references, invalidated evidence, remaining uncertainty and next '
                'verifiable action. Reuse applicable investigations; request only missing or '
                'changed facts through the current workers. An unchanged waiting state, elapsed '
                'time or repeated acknowledgement is not new evidence requiring investigation '
                'or expert consultation. A new failure, contradiction, operator instruction or '
                'changed dependency does require reassessment. An expert answer supplied again '
                'for the identical hash-bound question is retained advice: apply it, or name '
                'the new uncertainty/evidence in a changed question. Unknown validity warrants '
                'a scoped check, never a task stop. There is no consultation-count limit. ')
        if kind == 'coordinate' and request.get('live_coordination') == 1:
            instruction += (
                ' You retain coordination throughout execution. Delegate one bounded deliverable or '
                'a set of independent bounded deliverables, with ownership, settled design decisions, '
                'required evidence and an explicit handback boundary. Do not delegate the whole '
                'unresolved multi-module goal or your design/readiness authority to the worker root. '
                'The worker returns results and questions to this conversation after that boundary; '
                'then inspect the new evidence, settle design questions (consulting the existing expert '
                'when needed), and assign the next scope or route the candidate to independent review. '
                'Slice by coherent deliverable, not by fixed turn, time or token limits. '
                'Worker readiness is a proposal for your decision, not permission to skip you. ')
            if request.get('packet', {}).get('stage') == 'worker_running':
                from ._context_signals import CONTEXT_RENEWAL_GUIDANCE
                instruction += (' The worker turn is still executing. Settle the supplied question or deliverable '
                    'now; your instruction returns through the existing same-turn steer channel. '
                    'Use await_input only for an actual contract-level commander ruling; otherwise decide '
                    'within the existing authority and ask your configured expert when needed. '
                    'Do not spawn a second Foundry goal. ' + CONTEXT_RENEWAL_GUIDANCE)
        if kind == 'coordinate' and request.get('claude_live_events') == 1:
            instruction = instruction.replace('The worker turn is still executing.',
                'The worker execution remains open; its native turn may already have returned.')
            instruction = instruction.replace('existing same-turn steer channel.',
                'existing native delivery channel: Codex same-turn steer, Claude after its current '
                'native turn returns. Do not ask the Claude root to keep its turn open waiting for your answer.')
        if kind == 'coordinate' and request.get('member_tier_decisions'):
            instruction += (' Answer every supplied member_tier_decisions ID with an action and concrete reason. '
                'Escalate, retain, defer, or choose another justified continuation using the existing team controls; '
                'put the actual member instructions in instruction. A recorded answer does not itself prove that '
                'a member was reassigned. Do not repeat a failed request just to satisfy this accounting.')
        if kind == 'coordinate' and request.get('packet', {}).get('stage') == 'expert_consultation':
            instruction = ('Resolve the supplied expert question using the evidence and relevant source. '
                'Do not implement or start workers. Return action=delegate with your decision, supporting '
                'references and unresolved uncertainty in instruction. This is advice to the existing '
                'manager/worker, not independent final review or an acceptance verdict.')
        if request.get('context_reuse') == 1:
            instruction += EVIDENCE_GUIDANCE + (' Start recovery from manager_handoff.progress_note and retained artifact references. '
                'The full history remains available by reference: inspect only missing, changed or contradictory '
                'evidence, not every prior request. Before native compaction retain a compact note of decisions, '
                'source revisions, verified evidence, open questions and next action. Experts receive investigator '
                'file:line excerpts through the manager; ask for missing facts rather than bulk reading. '
                'Direct decisive reads remain available with #decide: concrete reason. Do not stop useful work '
                'or manufacture a new owner merely because the context grew.')
        if kind == 'coordinate' and request.get('expert_consultation') is True:
            instruction += (' For unresolved architecture/ownership decisions, conflicting correctness evidence '
                'or recurring reasoning defects, return consult_expert and a non-null expert_request naming '
                'the trigger, concrete question and evidence references. The runtime obtains a retained '
                'expert answer before your next decision. For other actions return expert_request=null. '
                'Use an already supplied expert_consultation answer rather than requesting it again; '
                'request another only for a different question or changed evidence. Mechanical failures, '
                'quota and index outages alone do not require experts.')
        if kind == 'coordinate' and request.get('design_convergence') == 1:
            instruction += (
                ' Design convergence and evidence reuse: scale planning to the actual uncertainty. '
                'For unsettled architecture, identify the decision, ownership boundary, competing '
                'hypotheses and a discriminating counterexample or smallest useful check before '
                'commissioning a full rewrite or long design. Proceed directly when the direction '
                'is already supported; do not create a mandatory planning or approval stage. '
                'In your existing instruction, retain a compact progress note: what changed, '
                'which decisions and evidence remain applicable, unresolved questions, and the '
                'next action with its expected new information or verifiable deliverable. '
                'Reference existing artifacts by path and source revision/hash or recorded test '
                'conditions when available; retain their limitations. Reuse conclusions only while '
                'their sources, assumptions and affected dependencies remain valid. Changed or '
                'contradictory evidence calls for the affected checks; broaden verification when '
                'dependency impact is uncertain. Missing provenance calls for a scoped source check, '
                'not automatic rejection or wholesale repetition. '
                'Give experts the concrete unresolved decision, relevant evidence pointers and '
                'changes since prior advice; ask for a decision, rationale and counterexample '
                'before expansive prose. Carry this brief through existing worker instructions '
                'and expert_request evidence_refs, without a second ledger. '
                'If a round adds no useful evidence or progress, diagnose why and choose a '
                'different probe, implementation approach, expert question or existing owner '
                'handoff. Continue useful work under existing authority; ask the operator only '
                'for a genuinely missing decision. Unknown failures retain their evidence and '
                'an actionable continuation. Do not cap rounds, downgrade by token count, '
                'repeat a resolved consultation, or treat reused evidence as independent review. '
                'Preserve required independent review and project acceptance.')
        if kind == 'coordinate' and request.get('review_convergence') == 1 and request.get('expert_consultation'):
            instruction += (
                ' Review non-convergence trigger: compare the last two consecutive review rounds in '
                'packet.review_history and your retained conversation. Judge the underlying failure '
                'class, not identical wording, file names or line numbers. If the same unresolved '
                'reasoning/ownership/design finding recurs, your next action must be consult_expert '
                'with expert_request.trigger=recurring_reasoning, describing the recurring pattern, '
                'attempted corrections, counterexample and both review evidence paths. Use the '
                'existing configured expert; do not silently change the worker model. This triggers '
                'a change of approach, not a stop or round cap. A supplied expert_consultation already '
                'settles this request: apply it before another consultation; only materially new '
                'evidence or a different unresolved question warrants another. Missing history is '
                'not a reason to stop: inspect available evidence and continue useful work. '
                'Distinct findings, mechanical edits, quota and index failures alone are not '
                'reasoning non-convergence.')
        if kind == 'coordinate' and request.get('instruction_provenance') == 1:
            instruction += (
                " Your instruction is authored by the manager. Label your own diagnosis and decisions as "
                "manager judgments, never as an initiator/operator ruling. Attribute operator instructions "
                "only to their supplied source (original order, operator_answer or referenced operator input); "
                "separate quotations from your interpretation. Exercise the authority already delegated by "
                "the order and repository rules. If a decision requires additional operator authority, "
                "delegate the concrete question through the existing worker input channel; do not invent "
                "approval to modify frozen contracts or owner-controlled scope.")
        if kind == 'coordinate' and request.get('artifact_delivery') is True:
            instruction += (
                " For an order whose deliverables are documents or data rather than a code candidate, "
                "after the worker returns successfully you may choose deliver_artifacts with the concrete "
                "file paths in artifacts and a handoff explanation in instruction. Inspect the deliverables "
                "against the order first. This records delivery pending owner acceptance, not independent "
                "review or task acceptance. Do not use it to bypass required code changes or code review. "
                "For delegate or review return artifacts as an empty list. Do not request another worker "
                "turn merely to create a code commit for an artifact-only order.")
        if kind == 'coordinate' and request.get('operator_input') is True:
            # Persisted pre-a33 requests keep their original prompt and schema.
            instruction = instruction.replace("delegate that question to the worker's existing input channel.",
                'return await_input with the exact question, evidence and required decision in instruction.')
            instruction = instruction.replace('delegate the concrete question through the existing worker input channel;',
                'return await_input with the concrete question;')
            instruction += (' The goal publishes that input request and stops model calls until an explicit answer '
                'continuation. Accepted/queued question delivery is not an answer. Never delegate another turn '
                'just to wait, repeat a question or investigate how to suspend. Continue independent authorized '
                'work first if useful work remains. Ordinary delegate prose cannot suspend execution. '
                'For await_input return artifacts=[] and expert_request=null when those fields are required.')
        if kind == "review":
            # The packet is frozen before native allocation. Historical packets
            # must render byte-for-byte for recovery's stdin hash comparison.
            if request.get("packet", {}).get("review_evidence_policy") == 1:
                instruction += (
                    " Start with the candidate handoff's evidence references and coverage: check their source "
                    "revision or dirty diff, relevant dependencies and assumptions against this candidate. "
                    "Reuse matching observations without repeating their tool runs; inspect changed code, "
                    "uncovered criteria and contradictory evidence yourself. Internal expert consultation "
                    "does not replace your independent verdict. Keep coverage notes in existing task evidence "
                    "with references instead of full logs; do not add fields to the response contract.")
            instruction += (
                " For a follow-up review, start with review_delta and the prior open findings. "
                "Read the referenced prior checkpoint or execution evidence only as needed. "
                "The full original base-to-candidate acceptance scope still applies; inspect affected "
                "consumers and widen the review when dependencies, assumptions or evidence changed. "
                "Do not reread entire execution logs merely to recreate task history.")
            if request.get("correction_routing") is True:
                instruction += (
                    " Also return correction_guidance for any remaining work: delivery means missing "
                    "deployment, environment or acceptance evidence; implementation means an understood "
                    "code/test fix; reasoning means an unresolved architectural or difficult reasoning "
                    "problem; none means no correction needed. For mixed findings choose the cause "
                    "that requires the most capable worker. Recommend retain when the current worker "
                    "can complete the work, expert when stronger reasoning is needed, and explain why. "
                    "BLOCK severity alone, waiting, or missing deployment evidence does not imply "
                    "the worker lacks capability. This advice does not change your verdict or findings.")
        if kind == 'coordinate' and request.get('retry_of'):
            instruction += (
                " This explicitly continues an interrupted manager decision in the original conversation. "
                "The request below is restated to identify the unfinished decision, not to start the task again. "
                "Use the retained decisions, tool results and completed worker evidence first; finish only "
                "the missing decision. Do not repeat completed worker actions or claim the interrupted turn succeeded.")
        # call_id belongs to the host's allocation/receipt, not the model's task.
        # Keep it there: paired independent reviews can send identical input
        # without merging their native sessions or reusing generated verdicts.
        envelope = {key: value for key, value in request.items() if key != "call_id"}
        envelope["response_contract"] = contract
        serialized = json.dumps(envelope, ensure_ascii=False, allow_nan=False,
                                sort_keys=True, separators=(",", ":"))
        return (
            "You are a repository-aware Foundry agent. The target repository is your current working "
            "directory. Choose what to read and how to investigate. "
            "Use existing result summaries and referenced evidence when their source, candidate and "
            "assumptions still apply; recheck changed or uncertain facts with focused checks. "
            "Keep large tool results in retrievable files and return the relevant findings and locations. "
            "When delegating, give each agent its question and relevant context; continue that agent with "
            "the changes and remaining questions instead of repeating the full task history. " + WAIT_GUIDANCE + " " + instruction +
            " Return exactly one final JSON object matching response_contract, with no extra fields.\n\n" +
            serialized
        ).encode("utf-8")
    except (CollaborationError, ValueError, UnicodeError, TypeError) as exc:
        raise native_error(f"invalid native request: {exc}") from exc
