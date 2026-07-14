from council_of_agents.scripts.task_dag import TaskDAG, TaskNode


def _task(task_id, *, read=None, write=None, declared=True):
    return TaskNode(
        id=task_id,
        description=task_id,
        read_scope=read or [],
        write_scope=write or [],
        read_scope_declared=declared,
        write_scope_declared=declared,
    )


def test_disjoint_writers_share_wave():
    tasks = [
        _task("A", read=["src/a.py"], write=["src/a.py"]),
        _task("B", read=["src/b.py"], write=["src/b.py"]),
    ]
    assert [t.id for t in TaskDAG.safe_execution_wave(tasks)] == ["A", "B"]


def test_overlapping_writers_are_serialized():
    tasks = [
        _task("A", read=["src"], write=["src/a.py"]),
        _task("B", read=["src/a.py"], write=["src/a.py"]),
    ]
    assert [t.id for t in TaskDAG.safe_execution_wave(tasks)] == ["A"]


def test_writer_and_overlapping_reader_are_serialized():
    writer = _task("A", read=[], write=["src"])
    reader = _task("B", read=["src/a.py"], write=[])
    assert TaskDAG.tasks_conflict(writer, reader) is True


def test_unknown_scopes_fail_closed():
    unknown = _task("A", declared=False)
    writer = _task("B", read=[], write=["src/b.py"])
    assert [t.id for t in TaskDAG.safe_execution_wave([unknown, writer])] == ["A"]


def test_explicit_read_only_tasks_can_run_together():
    tasks = [
        _task("A", read=["src/a.py"], write=[]),
        _task("B", read=["src/a.py"], write=[]),
    ]
    assert [t.id for t in TaskDAG.safe_execution_wave(tasks)] == ["A", "B"]


def test_unsafe_scope_strings_fail_closed():
    unsafe = _task("A", read=["../outside"], write=["C:/outside"])
    safe = _task("B", read=["src/b.py"], write=["src/b.py"])
    assert TaskDAG.tasks_conflict(unsafe, safe) is True
