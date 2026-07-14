from council_of_agents.scripts.task_dag import TaskDAG


def test_dependency_handoff_is_bounded_and_structured():
    dag = TaskDAG.from_task_list([
        {"id": "T1", "description": "investigate"},
        {
            "id": "T2",
            "description": "implement",
            "depends_on": ["T1"],
            "acceptance": "tests pass",
            "read_scope": ["src/a.py"],
            "write_scope": ["src/a.py"],
            "verification": {"adapter": "file", "config": {"path": "src/a.py"}},
        },
    ])
    raw = "START-" + ("x" * 5000) + "-END"
    dag.mark_done("T1", output=raw)

    packet = dag.build_work_packet("T2")
    summary = packet.dependency_results["T1"].summary
    assert len(summary) <= 1200
    assert summary.startswith("START-")
    assert summary.endswith("-END")
    assert "dependency output compacted" in summary
    assert packet.acceptance_ids == ["T2"]
    assert packet.read_scope == ["src/a.py"]
    assert packet.write_scope == ["src/a.py"]
    assert packet.verification.adapter == "file"
    assert raw not in packet.model_dump_json()


def test_work_packet_fields_survive_dag_round_trip():
    dag = TaskDAG.from_task_list([{
        "id": "T1",
        "description": "change file",
        "acceptance_ids": ["AC-7"],
        "read_scope": ["src/a.py"],
        "write_scope": ["src/a.py"],
        "artifact_refs": ["artifact-1"],
        "evidence_refs": ["evidence-1"],
        "base_hashes": {"src/a.py": "abc"},
    }])
    restored = TaskDAG.from_task_list(dag.to_dict()["nodes"])
    packet = restored.build_work_packet("T1")

    assert packet.acceptance_ids == ["AC-7"]
    assert packet.artifact_refs == ["artifact-1"]
    assert packet.evidence_refs == ["evidence-1"]
    assert packet.base_hashes == {"src/a.py": "abc"}
