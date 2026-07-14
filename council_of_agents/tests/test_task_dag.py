import pytest
from council_of_agents.scripts.task_dag import TaskDAG, TaskNode


class TestLinearDAG:
    def test_topological_order(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="first"))
        dag.add_task(TaskNode(id="T2", description="second", depends_on=["T1"]))
        dag.add_task(TaskNode(id="T3", description="third", depends_on=["T2"]))

        ready = dag.get_ready_tasks()
        assert len(ready) == 1
        assert ready[0].id == "T1"

    def test_sequential_progression(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="first"))
        dag.add_task(TaskNode(id="T2", description="second", depends_on=["T1"]))
        dag.add_task(TaskNode(id="T3", description="third", depends_on=["T2"]))

        dag.mark_done("T1")
        ready = dag.get_ready_tasks()
        assert len(ready) == 1
        assert ready[0].id == "T2"

        dag.mark_done("T2")
        ready = dag.get_ready_tasks()
        assert len(ready) == 1
        assert ready[0].id == "T3"


class TestDiamondDAG:
    def test_parallel_ready_after_root(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="root"))
        dag.add_task(TaskNode(id="T2", description="left", depends_on=["T1"]))
        dag.add_task(TaskNode(id="T3", description="right", depends_on=["T1"]))
        dag.add_task(TaskNode(id="T4", description="merge", depends_on=["T2", "T3"]))

        dag.mark_done("T1")
        ready = dag.get_ready_tasks()
        ready_ids = {n.id for n in ready}
        assert ready_ids == {"T2", "T3"}

    def test_merge_requires_both(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="root"))
        dag.add_task(TaskNode(id="T2", description="left", depends_on=["T1"]))
        dag.add_task(TaskNode(id="T3", description="right", depends_on=["T1"]))
        dag.add_task(TaskNode(id="T4", description="merge", depends_on=["T2", "T3"]))

        dag.mark_done("T1")
        dag.mark_done("T2")
        ready = dag.get_ready_tasks()
        ready_ids = {n.id for n in ready}
        assert ready_ids == {"T3"}
        assert "T4" not in ready_ids

    def test_merge_ready_when_both_done(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="root"))
        dag.add_task(TaskNode(id="T2", description="left", depends_on=["T1"]))
        dag.add_task(TaskNode(id="T3", description="right", depends_on=["T1"]))
        dag.add_task(TaskNode(id="T4", description="merge", depends_on=["T2", "T3"]))

        dag.mark_done("T1")
        dag.mark_done("T2")
        dag.mark_done("T3")
        ready = dag.get_ready_tasks()
        assert len(ready) == 1
        assert ready[0].id == "T4"


class TestComplexDAG:
    def setup_method(self):
        self.dag = TaskDAG()
        self.dag.add_task(TaskNode(id="A", description="layer 0"))
        self.dag.add_task(TaskNode(id="B", description="layer 1a", depends_on=["A"]))
        self.dag.add_task(TaskNode(id="C", description="layer 1b", depends_on=["A"]))
        self.dag.add_task(TaskNode(id="D", description="layer 2a", depends_on=["B"]))
        self.dag.add_task(TaskNode(id="E", description="layer 2b", depends_on=["B", "C"]))
        self.dag.add_task(TaskNode(id="F", description="layer 3", depends_on=["D", "E"]))

    def test_initial_ready_is_root(self):
        ready = self.dag.get_ready_tasks()
        assert len(ready) == 1
        assert ready[0].id == "A"

    def test_parallel_at_layer_1(self):
        self.dag.mark_done("A")
        ready = self.dag.get_ready_tasks()
        ready_ids = {n.id for n in ready}
        assert ready_ids == {"B", "C"}

    def test_partial_layer_2_ready(self):
        self.dag.mark_done("A")
        self.dag.mark_done("B")
        ready = self.dag.get_ready_tasks()
        ready_ids = {n.id for n in ready}
        assert ready_ids == {"C", "D"}

    def test_final_node_after_all_deps(self):
        self.dag.mark_done("A")
        self.dag.mark_done("B")
        self.dag.mark_done("C")
        self.dag.mark_done("D")
        self.dag.mark_done("E")
        ready = self.dag.get_ready_tasks()
        assert len(ready) == 1
        assert ready[0].id == "F"

    def test_full_completion(self):
        for nid in ["A", "B", "C", "D", "E", "F"]:
            self.dag.mark_done(nid)
        assert self.dag.all_complete()


class TestSelfDependency:
    def test_self_dependency_raises(self):
        dag = TaskDAG()
        with pytest.raises(ValueError, match="Self-dependency"):
            dag.add_task(TaskNode(id="T1", description="bad", depends_on=["T1"]))


class TestUnknownDependency:
    def test_unknown_dep_raises(self):
        dag = TaskDAG()
        with pytest.raises(ValueError, match="Unknown dependency"):
            dag.add_task(TaskNode(id="T1", description="bad", depends_on=["MISSING"]))


class TestDuplicateTaskID:
    def test_duplicate_id_raises(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="first"))
        with pytest.raises(ValueError, match="Duplicate task ID"):
            dag.add_task(TaskNode(id="T1", description="duplicate"))

    def test_dag_unchanged_after_duplicate(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="first"))
        with pytest.raises(ValueError):
            dag.add_task(TaskNode(id="T1", description="duplicate"))
        assert len(dag._nodes) == 1


class TestCycleDetection:
    def test_three_node_cycle_detected(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="a"))
        dag.add_task(TaskNode(id="T2", description="b", depends_on=["T1"]))
        dag.add_task(TaskNode(id="T3", description="c", depends_on=["T2"]))
        dag._adj["T3"].append("T1")
        cycle = dag._detect_cycle_from("T1")
        assert cycle is not None
        assert set(cycle) == {"T1", "T2", "T3"}

    def test_cycle_path_is_ordered(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="a"))
        dag.add_task(TaskNode(id="T2", description="b", depends_on=["T1"]))
        dag.add_task(TaskNode(id="T3", description="c", depends_on=["T2"]))
        dag._adj["T3"].append("T1")
        cycle = dag._detect_cycle_from("T1")
        assert cycle[0] == cycle[-1]

    def test_add_task_rejects_cycle_via_duplicate(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="a"))
        dag.add_task(TaskNode(id="T2", description="b", depends_on=["T1"]))
        dag.add_task(TaskNode(id="T3", description="c", depends_on=["T2"]))
        with pytest.raises(ValueError, match="Duplicate task ID"):
            dag.add_task(TaskNode(id="T1", description="a", depends_on=["T3"]))


class TestNoCycleOnValidGraph:
    def test_diamond_is_valid(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="root"))
        dag.add_task(TaskNode(id="T2", description="left", depends_on=["T1"]))
        dag.add_task(TaskNode(id="T3", description="right", depends_on=["T1"]))
        dag.add_task(TaskNode(id="T4", description="merge", depends_on=["T2", "T3"]))
        assert len(dag._nodes) == 4


class TestInProgressBlocksReady:
    def test_empty_when_in_progress(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="a"))
        dag.add_task(TaskNode(id="T2", description="b", depends_on=["T1"]))
        dag.mark_done("T1")
        dag._nodes["T2"].status = "IN_PROGRESS"
        assert dag.get_ready_tasks() == []

    def test_single_in_progress_blocks_all(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="a"))
        dag.add_task(TaskNode(id="T2", description="b"))
        dag.mark_done("T1")
        dag._nodes["T2"].status = "IN_PROGRESS"
        assert dag.get_ready_tasks() == []


class TestMarkDoneStoresOutput:
    def test_output_field_set(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="a"))
        dag.mark_done("T1", output="result data")
        assert dag._nodes["T1"].output == "result data"

    def test_status_set_to_done(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="a"))
        dag.mark_done("T1")
        assert dag._nodes["T1"].status == "DONE"


class TestMarkFailed:
    def test_status_set_to_failed(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="a"))
        dag.mark_failed("T1")
        assert dag._nodes["T1"].status == "FAILED"

    def test_mark_failed_unknown_raises(self):
        dag = TaskDAG()
        with pytest.raises(ValueError, match="Unknown task"):
            dag.mark_failed("NOPE")

    def test_mark_done_unknown_raises(self):
        dag = TaskDAG()
        with pytest.raises(ValueError, match="Unknown task"):
            dag.mark_done("NOPE")


class TestAllComplete:
    def test_empty_dag_is_complete(self):
        dag = TaskDAG()
        assert dag.all_complete() is True

    def test_not_complete_when_pending(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="a"))
        assert dag.all_complete() is False

    def test_complete_when_all_done(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="a"))
        dag.add_task(TaskNode(id="T2", description="b"))
        dag.mark_done("T1")
        dag.mark_done("T2")
        assert dag.all_complete() is True

    def test_complete_with_mixed_done_failed(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="a"))
        dag.add_task(TaskNode(id="T2", description="b"))
        dag.mark_done("T1")
        dag.mark_failed("T2")
        assert dag.all_complete() is True

    def test_not_complete_with_in_progress(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="a"))
        dag._nodes["T1"].status = "IN_PROGRESS"
        assert dag.all_complete() is False


class TestToDict:
    def test_structure(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="root"))
        dag.add_task(TaskNode(id="T2", description="child", depends_on=["T1"]))
        result = dag.to_dict()
        assert "nodes" in result
        assert "edges" in result

    def test_nodes_content(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="root"))
        dag.add_task(TaskNode(id="T2", description="child", depends_on=["T1"]))
        result = dag.to_dict()
        node_ids = {n["id"] for n in result["nodes"]}
        assert node_ids == {"T1", "T2"}

    def test_edges_content(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="root"))
        dag.add_task(TaskNode(id="T2", description="child", depends_on=["T1"]))
        result = dag.to_dict()
        assert len(result["edges"]) == 1
        edge = result["edges"][0]
        assert edge["from"] == "T1"
        assert edge["to"] == "T2"

    def test_node_fields(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="root"))
        dag.mark_done("T1", output="out")
        result = dag.to_dict()
        node = result["nodes"][0]
        assert node["id"] == "T1"
        assert node["description"] == "root"
        assert node["depends_on"] == []
        assert node["status"] == "DONE"
        assert node["output"] == "out"


class TestFromTaskList:
    def test_basic_build(self):
        tasks = [
            {"id": "T1", "description": "root"},
            {"id": "T2", "description": "child", "depends_on": ["T1"]},
        ]
        dag = TaskDAG.from_task_list(tasks)
        assert len(dag._nodes) == 2
        assert "T1" in dag._nodes
        assert "T2" in dag._nodes

    def test_default_description(self):
        tasks = [{"id": "T1"}]
        dag = TaskDAG.from_task_list(tasks)
        assert dag._nodes["T1"].description == ""

    def test_default_depends_on(self):
        tasks = [{"id": "T1"}]
        dag = TaskDAG.from_task_list(tasks)
        assert dag._nodes["T1"].depends_on == []

    def test_ready_tasks_after_build(self):
        tasks = [
            {"id": "T1", "description": "root"},
            {"id": "T2", "description": "child", "depends_on": ["T1"]},
        ]
        dag = TaskDAG.from_task_list(tasks)
        ready = dag.get_ready_tasks()
        assert len(ready) == 1
        assert ready[0].id == "T1"

    def test_diamond_from_list(self):
        tasks = [
            {"id": "T1", "description": "root"},
            {"id": "T2", "description": "left", "depends_on": ["T1"]},
            {"id": "T3", "description": "right", "depends_on": ["T1"]},
            {"id": "T4", "description": "merge", "depends_on": ["T2", "T3"]},
        ]
        dag = TaskDAG.from_task_list(tasks)
        assert len(dag._nodes) == 4
        dag.mark_done("T1")
        ready_ids = {n.id for n in dag.get_ready_tasks()}
        assert ready_ids == {"T2", "T3"}


class TestTaskRetry:
    def test_mark_retryable(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="root", max_retries=2))
        
        # Initially pending
        assert dag._nodes["T1"].status == "PENDING"
        assert dag._nodes["T1"].retry_count == 0
        
        # Mark failed first
        dag.mark_failed("T1", "some error")
        assert dag._nodes["T1"].status == "FAILED"
        assert dag._nodes["T1"].reason == "some error"
        
        # Can retry
        assert dag.mark_retryable("T1", "some error") is True
        assert dag._nodes["T1"].status == "PENDING"
        assert dag._nodes["T1"].retry_count == 1
        assert dag._nodes["T1"].error_history == ["some error"]
        
        # Mark failed again
        dag.mark_failed("T1", "another error")
        
        # Can retry again
        assert dag.mark_retryable("T1", "another error") is True
        assert dag._nodes["T1"].status == "PENDING"
        assert dag._nodes["T1"].retry_count == 2
        
        # Mark failed third time
        dag.mark_failed("T1", "third error")
        
        # Cannot retry anymore (max_retries reached)
        assert dag.mark_retryable("T1", "third error") is False
        assert dag._nodes["T1"].status == "FAILED"


class TestFailurePropagation:
    def test_propagate_failures_blocked(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="root", max_retries=1))
        dag.add_task(TaskNode(id="T2", description="child", depends_on=["T1"]))
        
        # T1 fails permanently (0 retries remaining)
        dag.mark_failed("T1", "fatal")
        dag._nodes["T1"].retry_count = 1  # exhausted
        
        # Propagate
        dag.propagate_failures()
        assert dag._nodes["T2"].status == "BLOCKED"
        assert "Dependency T1 failed permanently" in dag._nodes["T2"].reason
        
        # Check all complete
        assert dag.all_complete() is True


class TestTransitiveBlockPropagation:
    def test_transitive_block_through_chain(self):
        """T1→T2→T3: T1 fails permanently → T2 blocked → T3 blocked."""
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="root", max_retries=1))
        dag.add_task(TaskNode(id="T2", description="mid", depends_on=["T1"]))
        dag.add_task(TaskNode(id="T3", description="leaf", depends_on=["T2"]))
        dag.mark_failed("T1", "fatal")
        dag._nodes["T1"].retry_count = 1  # exhausted
        dag.propagate_failures()
        assert dag._nodes["T2"].status == "BLOCKED"
        assert dag._nodes["T3"].status == "BLOCKED"

    def test_transitive_block_diamond(self):
        """T1→T2,T3→T4: T1 fails → all downstream blocked."""
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="root", max_retries=1))
        dag.add_task(TaskNode(id="T2", description="left", depends_on=["T1"]))
        dag.add_task(TaskNode(id="T3", description="right", depends_on=["T1"]))
        dag.add_task(TaskNode(id="T4", description="merge", depends_on=["T2", "T3"]))
        dag.mark_failed("T1", "fatal")
        dag._nodes["T1"].retry_count = 1
        dag.propagate_failures()
        assert dag._nodes["T2"].status == "BLOCKED"
        assert dag._nodes["T3"].status == "BLOCKED"
        assert dag._nodes["T4"].status == "BLOCKED"

    def test_no_block_when_dep_retryable(self):
        """T1 has retries left → T2 stays PENDING."""
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="root", max_retries=2))
        dag.add_task(TaskNode(id="T2", description="child", depends_on=["T1"]))
        dag.mark_failed("T1", "retryable")
        dag.propagate_failures()
        assert dag._nodes["T2"].status == "PENDING"

    def test_ready_after_dep_succeeds_on_retry(self):
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="root", max_retries=2))
        dag.add_task(TaskNode(id="T2", description="child", depends_on=["T1"]))
        dag.mark_failed("T1", "error")
        dag.mark_retryable("T1")
        dag.mark_done("T1", "fixed")
        ready = dag.get_ready_tasks()
        assert len(ready) == 1
        assert ready[0].id == "T2"

    def test_get_ready_tasks_stays_pure(self):
        """get_ready_tasks must NOT modify node statuses (CQS)."""
        dag = TaskDAG()
        dag.add_task(TaskNode(id="T1", description="root", max_retries=1))
        dag.add_task(TaskNode(id="T2", description="child", depends_on=["T1"]))
        dag.mark_failed("T1", "fatal")
        dag._nodes["T1"].retry_count = 1
        # Don't call propagate_failures — T2 should stay PENDING
        ready = dag.get_ready_tasks()
        assert dag._nodes["T2"].status == "PENDING"
        assert len(ready) == 0

