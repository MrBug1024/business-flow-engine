from __future__ import annotations

import importlib.util
import json
import unittest
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    PROJECT_ROOT
    / "system_skills"
    / "derive-business-flow"
    / "scripts"
    / "derive_business_flow.py"
)
SPEC = importlib.util.spec_from_file_location("derive_business_flow_skill", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
FLOW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FLOW)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def relation_fixture(root: Path) -> Path:
    relation_root = root / "data-relations"
    relation_root.mkdir(parents=True)
    result = {
        "schema_version": 1,
        "status": "complete",
        "scenario": {"name": "规则驱动业务判定", "purpose": "形成可追溯的业务判断结果"},
        "nodes": [
            {
                "id": "n_request",
                "name": "业务请求与上下文",
                "type": "input",
                "description": "发起判断所需的业务信息",
                "evidence_ids": ["E-REQ"],
            },
            {
                "id": "n_rules",
                "name": "业务判定规则",
                "type": "rule",
                "description": "约束判断形成的适用规则",
                "evidence_ids": ["E-RULE"],
            },
            {
                "id": "n_decision",
                "name": "规则驱动判定",
                "type": "decision",
                "description": "基于上下文和规则形成判断",
                "evidence_ids": ["E-DEC"],
            },
            {
                "id": "n_result",
                "name": "业务判定结果",
                "type": "output",
                "description": "供下游使用的场景结果",
                "evidence_ids": ["E-RES"],
            },
        ],
        "edges": [
            {
                "id": "e_feed",
                "source": "n_request",
                "target": "n_decision",
                "type": "feeds",
                "label": "提供判断上下文",
                "confidence": 0.92,
                "evidence_ids": ["E-FEED"],
            },
            {
                "id": "e_govern",
                "source": "n_rules",
                "target": "n_decision",
                "type": "governs",
                "label": "约束判断",
                "confidence": 0.95,
                "evidence_ids": ["E-GOV"],
            },
            {
                "id": "e_derive",
                "source": "n_decision",
                "target": "n_result",
                "type": "derives",
                "label": "形成结果",
                "confidence": 0.94,
                "evidence_ids": ["E-DER"],
            },
        ],
        "main_chain": ["n_request", "n_decision", "n_result"],
        "primary_data_path": ["n_request", "n_decision", "n_result"],
    }
    relations = relation_root / "scenario-relationship.json"
    write_json(relations, result)
    (relation_root / "relations.mmd").write_text("flowchart LR\n", encoding="utf-8")
    (relation_root / "relation-report.md").write_text("# relations\n", encoding="utf-8")
    return relations


def support(nodes: list[str], edges: list[str], evidence: list[str]) -> dict:
    return {
        "upstream_node_ids": nodes,
        "upstream_edge_ids": edges,
        "evidence_ids": evidence,
    }


def inference(rationale: str, confidence: float = 0.88) -> dict:
    return {"basis": "structural", "confidence": confidence, "rationale": rationale}


def valid_claims(template: dict) -> dict:
    claims = json.loads(json.dumps(template, ensure_ascii=False))
    claims["scenario"]["business_outcome"] = "形成受规则约束且可追溯的业务判定结果"
    claims["stages"] = [
        {
            "id": "s_intake",
            "name": "确认业务请求",
            "stage_type": "initiation",
            "objective": "确认进入场景的业务请求和必要上下文",
            "outcome": "形成可供判定使用的业务输入",
            "owner_role": "",
            "input_node_ids": ["n_request"],
            "output_node_ids": ["n_request"],
            "support": support(["n_request"], ["e_feed"], ["E-REQ", "E-FEED"]),
            "inference": inference("业务请求向判定提供上下文，先综合为场景启动责任。", 0.82),
        },
        {
            "id": "s_assess",
            "name": "执行规则驱动判定",
            "stage_type": "decision",
            "objective": "基于业务上下文和适用规则形成判断",
            "outcome": "形成可供结果落实使用的判定结论",
            "owner_role": "",
            "input_node_ids": ["n_request", "n_rules"],
            "output_node_ids": ["n_decision"],
            "support": support(
                ["n_request", "n_rules", "n_decision"],
                ["e_feed", "e_govern"],
                ["E-FEED", "E-GOV", "E-DEC"],
            ),
            "inference": inference("上下文汇入判定且业务规则约束判定，形成稳定判断责任。", 0.91),
        },
        {
            "id": "s_result",
            "name": "形成并闭环业务结果",
            "stage_type": "closure",
            "objective": "将判定结论转化为可使用的场景级结果",
            "outcome": "业务判定结果形成并可供后续使用",
            "owner_role": "",
            "input_node_ids": ["n_decision"],
            "output_node_ids": ["n_result"],
            "support": support(
                ["n_decision", "n_result"], ["e_derive"], ["E-DER", "E-RES"]
            ),
            "inference": inference("上游判定通过 derives 关系形成最终输出，支撑结果闭环。", 0.93),
        },
    ]
    claims["transitions"] = [
        {
            "id": "t_intake_assess",
            "source": "s_intake",
            "target": "s_assess",
            "type": "normal",
            "label": "业务上下文进入判定",
            "condition": "",
            "support": support(
                ["n_request", "n_decision"], ["e_feed"], ["E-FEED"]
            ),
            "inference": inference("feeds 关系表明已准备的业务上下文进入判断责任。", 0.84),
        },
        {
            "id": "t_assess_result",
            "source": "s_assess",
            "target": "s_result",
            "type": "normal",
            "label": "判定结论形成业务结果",
            "condition": "",
            "support": support(
                ["n_decision", "n_result"], ["e_derive"], ["E-DER"]
            ),
            "inference": inference("derives 关系表明判断先于场景结果形成。", 0.92),
        },
    ]
    claims["main_flow"] = ["s_intake", "s_assess", "s_result"]
    claims["states"] = [
        {
            "id": "st_complete",
            "name": "业务结果已形成",
            "state_type": "terminal",
            "reached_after": "s_result",
            "meaning": "场景级结果已经生成",
            "support": support(["n_result"], ["e_derive"], ["E-DER", "E-RES"]),
            "inference": inference("最终输出及其形成关系共同支撑该终态。", 0.93),
        }
    ]
    claims["controls"] = [
        {
            "id": "c_rules",
            "name": "业务判定规则",
            "applies_to": ["s_assess"],
            "policy": "约束判定结论形成",
            "support": support(["n_rules"], ["e_govern"], ["E-RULE", "E-GOV"]),
        }
    ]
    claims["validation_checks"] = [
        {
            "id": "v_trace",
            "target_kind": "state",
            "target_id": "st_complete",
            "method": "input_output_traceability",
            "role": "validate_not_define",
            "question": "历史结果能否追溯到对应业务输入？",
            "pass_signal": "抽样结果均可通过已验收关系追溯到输入",
            "source_node_ids": ["n_request", "n_result"],
            "limitation": "只验证可追溯性，不定义标准流程。",
        }
    ]
    claims["open_questions"] = []
    claims["coverage"]["used_upstream_node_ids"] = [
        "n_request",
        "n_rules",
        "n_decision",
        "n_result",
    ]
    claims["coverage"]["used_upstream_edge_ids"] = ["e_feed", "e_govern", "e_derive"]
    claims["coverage"]["context_only"] = []
    return claims


class DeriveBusinessFlowSkillTests(unittest.TestCase):
    def test_prepare_blocks_without_complete_upstream_artifacts(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "business-flow"
            code, payload = FLOW.prepare(
                Namespace(relations=str(root / "missing.json"), output=str(output), summary_limit=20)
            )
            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "blocked_missing_or_invalid_relations")
            self.assertTrue((output / "prepare-status.json").is_file())
            self.assertFalse((output / "flow-brief.json").exists())

    def test_prepare_preflight_and_finalize_macro_flow(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            relations = relation_fixture(root)
            output = root / "business-flow"
            prepare_code, prepare_payload = FLOW.prepare(
                Namespace(relations=str(relations), output=str(output), summary_limit=20)
            )
            self.assertEqual(prepare_code, 0, prepare_payload)
            template = json.loads((output / "flow-claims.template.json").read_text(encoding="utf-8"))
            candidate = output / "flow-claims.candidate.json"
            write_json(candidate, valid_claims(template))
            arguments = Namespace(
                relations=str(relations), output=str(output), claims=str(candidate), summary_limit=20
            )
            preflight_code, preflight_payload = FLOW.preflight(arguments)
            self.assertEqual(preflight_code, 0, preflight_payload)
            finalize_code, finalize_payload = FLOW.finalize(arguments)
            self.assertEqual(finalize_code, 0, finalize_payload)
            result = json.loads((output / "business-flow.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["history_policy"]["role"], "validation_only")
            self.assertEqual(len(result["main_flow"]), 3)
            self.assertTrue((output / "business-flow.mmd").is_file())
            self.assertTrue((output / "business-flow-report.md").is_file())
            self.assertFalse((output / "validation-errors.json").exists())

    def test_preflight_rejects_micro_history_defined_flow(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            relations = relation_fixture(root)
            output = root / "business-flow"
            FLOW.prepare(Namespace(relations=str(relations), output=str(output), summary_limit=20))
            template = json.loads((output / "flow-claims.template.json").read_text(encoding="utf-8"))
            claims = valid_claims(template)
            claims["stages"][0]["name"] = "逐条读取 claim_id 字段"
            claims["history_policy"]["role"] = "define_flow"
            candidate = output / "flow-claims.candidate.json"
            write_json(candidate, claims)
            code, payload = FLOW.preflight(
                Namespace(relations=str(relations), output=str(output), claims=str(candidate), summary_limit=20)
            )
            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "validation_failed")
            self.assertTrue(any("宏观业务阶段" in item for item in payload["errors"]))
            self.assertTrue(any("validation_only" in item for item in payload["errors"]))

    def test_stale_upstream_fingerprint_blocks_old_candidate_and_result(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            relations = relation_fixture(root)
            output = root / "business-flow"
            FLOW.prepare(Namespace(relations=str(relations), output=str(output), summary_limit=20))
            template = json.loads((output / "flow-claims.template.json").read_text(encoding="utf-8"))
            candidate = output / "flow-claims.candidate.json"
            write_json(candidate, valid_claims(template))
            arguments = Namespace(
                relations=str(relations), output=str(output), claims=str(candidate), summary_limit=20
            )
            finalize_code, finalize_payload = FLOW.finalize(arguments)
            self.assertEqual(finalize_code, 0, finalize_payload)

            changed = json.loads(relations.read_text(encoding="utf-8"))
            changed["scenario"]["purpose"] = "已经变化的场景范围"
            write_json(relations, changed)
            preflight_code, preflight_payload = FLOW.preflight(arguments)
            self.assertEqual(preflight_code, 2)
            self.assertTrue(any("fingerprint" in item for item in preflight_payload["errors"]))
            with self.assertRaises(FLOW.ContractError):
                FLOW.summary(
                    Namespace(
                        result=str(output / "business-flow.json"),
                        relations=str(relations),
                        offset=0,
                        limit=20,
                    )
                )


if __name__ == "__main__":
    unittest.main()
