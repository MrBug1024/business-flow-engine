"""文件型持久化层。

每个业务场景独立成目录，结构如下：

    data/scenarios/<scenario_id>/
        meta.json          业务场景元信息（名称、状态、表结构、关联、流程、技能索引）
        chat.jsonl         蒸馏通道对话记录
        verify_chat.jsonl  验证通道对话记录（独立，与蒸馏通道隔离）
        uploads/<file>     上传的业务数据表
        skills/<skill_id>/ 生成的技能（SKILL.md + scripts/）
        outputs/<file>     产出复刻文件

存储层只负责「读写与定位」，不包含任何推导逻辑。
"""

from __future__ import annotations

import json
import shutil
import threading
import uuid
from pathlib import Path
from time import time
from typing import Optional

from .config import settings
from .models import ChatMessage, Scenario, ScenarioStatus


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class ScenarioStore:
    """业务场景仓储。线程安全（进程内粗粒度锁），适配单机部署。"""

    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = (root or settings.data_path) / "scenarios"
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 路径
    def scenario_dir(self, scenario_id: str) -> Path:
        return self._root / scenario_id

    def uploads_dir(self, scenario_id: str) -> Path:
        path = self.scenario_dir(scenario_id) / "uploads"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def verify_uploads_dir(self, scenario_id: str) -> Path:
        """验证通道专用的「新业务数据」目录，与蒸馏通道的 uploads/ 完全分开。

        验证通道的意义是证明技能包能在**新数据**上跑通，而不是复读蒸馏阶段
        用过的同一批文件——两者必须是物理上不同的目录。
        """
        path = self.scenario_dir(scenario_id) / "verify_uploads"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def skills_dir(self, scenario_id: str) -> Path:
        path = self.scenario_dir(scenario_id) / "skills"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def outputs_dir(self, scenario_id: str) -> Path:
        """产出复刻文件的落盘目录（执行/校验结果文件，供前端下载/预览）。"""
        path = self.scenario_dir(scenario_id) / "outputs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _meta_file(self, scenario_id: str) -> Path:
        return self.scenario_dir(scenario_id) / "meta.json"

    def _chat_file(self, scenario_id: str) -> Path:
        return self.scenario_dir(scenario_id) / "chat.jsonl"

    def _verify_chat_file(self, scenario_id: str) -> Path:
        return self.scenario_dir(scenario_id) / "verify_chat.jsonl"

    # ------------------------------------------------------------- 场景 CRUD
    def create(self, name: str, description: str = "") -> Scenario:
        with self._lock:
            scenario = Scenario(id=_new_id("sc"), name=name, description=description)
            self.scenario_dir(scenario.id).mkdir(parents=True, exist_ok=True)
            self._write_meta(scenario)
            return scenario

    def list(self) -> list[Scenario]:
        with self._lock:
            scenarios: list[Scenario] = []
            for meta_file in self._root.glob("*/meta.json"):
                try:
                    scenarios.append(self._read_meta(meta_file))
                except Exception:  # noqa: BLE001  跳过损坏的元数据，保证列表可用
                    continue
            scenarios.sort(key=lambda s: s.created_at, reverse=True)
            return scenarios

    def get(self, scenario_id: str) -> Optional[Scenario]:
        with self._lock:
            meta_file = self._meta_file(scenario_id)
            if not meta_file.exists():
                return None
            return self._read_meta(meta_file)

    def save(self, scenario: Scenario) -> Scenario:
        """整体回写场景元信息（更新 updated_at）。"""
        with self._lock:
            scenario.updated_at = time()
            self._write_meta(scenario)
            return scenario

    def delete(self, scenario_id: str) -> bool:
        with self._lock:
            target = self.scenario_dir(scenario_id)
            if not target.exists():
                return False
            shutil.rmtree(target, ignore_errors=True)
            return True

    def set_status(self, scenario_id: str, status: ScenarioStatus) -> Optional[Scenario]:
        with self._lock:
            scenario = self.get(scenario_id)
            if scenario is None:
                return None
            scenario.status = status
            return self.save(scenario)

    # --------------------------------------------------------------- 对话记录
    def append_message(self, scenario_id: str, message: ChatMessage) -> None:
        with self._lock:
            chat_file = self._chat_file(scenario_id)
            chat_file.parent.mkdir(parents=True, exist_ok=True)
            with chat_file.open("a", encoding="utf-8") as fp:
                fp.write(message.model_dump_json() + "\n")

    def get_messages(self, scenario_id: str) -> list[ChatMessage]:
        with self._lock:
            chat_file = self._chat_file(scenario_id)
            if not chat_file.exists():
                return []
            messages: list[ChatMessage] = []
            for line in chat_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(ChatMessage.model_validate_json(line))
                except Exception:  # noqa: BLE001
                    continue
            return messages

    # ------------------------------------------------------- 验证通道对话记录
    def append_verify_message(self, scenario_id: str, message: ChatMessage) -> None:
        with self._lock:
            vf = self._verify_chat_file(scenario_id)
            vf.parent.mkdir(parents=True, exist_ok=True)
            with vf.open("a", encoding="utf-8") as fp:
                fp.write(message.model_dump_json() + "\n")

    def get_verify_messages(self, scenario_id: str) -> list[ChatMessage]:
        with self._lock:
            vf = self._verify_chat_file(scenario_id)
            if not vf.exists():
                return []
            messages: list[ChatMessage] = []
            for line in vf.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(ChatMessage.model_validate_json(line))
                except Exception:  # noqa: BLE001
                    continue
            return messages

    # ----------------------------------------------------------------- 内部
    def _write_meta(self, scenario: Scenario) -> None:
        self._meta_file(scenario.id).write_text(
            scenario.model_dump_json(indent=2), encoding="utf-8"
        )

    def _read_meta(self, meta_file: Path) -> Scenario:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
        return Scenario.model_validate(data)


# 全局唯一仓储实例
store = ScenarioStore()
