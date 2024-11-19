# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

from __future__ import annotations

import datetime as dt
import itertools
import os
from unittest import mock

import pendulum
import pytest

from airflow.jobs.job import Job
from airflow.jobs.triggerer_job_runner import TriggererJobRunner
from airflow.models import DagRun, TaskInstance
from airflow.models.baseoperator import BaseOperator
from airflow.models.dagbag import DagBag
from airflow.models.renderedtifields import RenderedTaskInstanceFields as RTIF
from airflow.models.taskinstancehistory import TaskInstanceHistory
from airflow.models.taskmap import TaskMap
from airflow.models.trigger import Trigger
from airflow.utils.platform import getuser
from airflow.utils.state import State, TaskInstanceState
from airflow.utils.timezone import datetime
from airflow.utils.types import DagRunType

from tests_common.test_utils.db import clear_db_runs, clear_rendered_ti_fields
from tests_common.test_utils.mock_operators import MockOperator

pytestmark = pytest.mark.db_test


DEFAULT = datetime(2020, 1, 1)
DEFAULT_DATETIME_STR_1 = "2020-01-01T00:00:00+00:00"
DEFAULT_DATETIME_STR_2 = "2020-01-02T00:00:00+00:00"

DEFAULT_DATETIME_1 = dt.datetime.fromisoformat(DEFAULT_DATETIME_STR_1)
DEFAULT_DATETIME_2 = dt.datetime.fromisoformat(DEFAULT_DATETIME_STR_2)


class TestTaskInstanceEndpoint:
    def setup_method(self):
        clear_db_runs()

    def teardown_method(self):
        clear_db_runs()

    @pytest.fixture(autouse=True)
    def setup_attrs(self, session) -> None:
        self.default_time = DEFAULT
        self.ti_init = {
            "logical_date": self.default_time,
            "state": State.RUNNING,
        }
        self.ti_extras = {
            "start_date": self.default_time + dt.timedelta(days=1),
            "end_date": self.default_time + dt.timedelta(days=2),
            "pid": 100,
            "duration": 10000,
            "pool": "default_pool",
            "queue": "default_queue",
        }
        clear_db_runs()
        clear_rendered_ti_fields()
        dagbag = DagBag(include_examples=True, read_dags_from_db=False)
        dagbag.sync_to_db()
        self.dagbag = dagbag

    def create_task_instances(
        self,
        session,
        dag_id: str = "example_python_operator",
        update_extras: bool = True,
        task_instances=None,
        dag_run_state=State.RUNNING,
        with_ti_history=False,
    ):
        """Method to create task instances using kwargs and default arguments"""

        dag = self.dagbag.get_dag(dag_id)
        tasks = dag.tasks
        counter = len(tasks)
        if task_instances is not None:
            counter = min(len(task_instances), counter)

        run_id = "TEST_DAG_RUN_ID"
        logical_date = self.ti_init.pop("logical_date", self.default_time)
        dr = None

        tis = []
        for i in range(counter):
            if task_instances is None:
                pass
            elif update_extras:
                self.ti_extras.update(task_instances[i])
            else:
                self.ti_init.update(task_instances[i])

            if "logical_date" in self.ti_init:
                run_id = f"TEST_DAG_RUN_ID_{i}"
                logical_date = self.ti_init.pop("logical_date")
                dr = None

            if not dr:
                dr = DagRun(
                    run_id=run_id,
                    dag_id=dag_id,
                    logical_date=logical_date,
                    run_type=DagRunType.MANUAL,
                    state=dag_run_state,
                )
                session.add(dr)
            ti = TaskInstance(task=tasks[i], **self.ti_init)
            session.add(ti)
            ti.dag_run = dr
            ti.note = "placeholder-note"

            for key, value in self.ti_extras.items():
                setattr(ti, key, value)
            tis.append(ti)

        session.commit()
        if with_ti_history:
            for ti in tis:
                ti.try_number = 1
                session.merge(ti)
            session.commit()
            dag.clear()
            for ti in tis:
                ti.try_number = 2
                ti.queue = "default_queue"
                session.merge(ti)
            session.commit()
        return tis


class TestGetTaskInstance(TestTaskInstanceEndpoint):
    def test_should_respond_200(self, test_client, session):
        self.create_task_instances(session)
        # Update ti and set operator to None to
        # test that operator field is nullable.
        # This prevents issue when users upgrade to 2.0+
        # from 1.10.x
        # https://github.com/apache/airflow/issues/14421
        session.query(TaskInstance).update({TaskInstance.operator: None}, synchronize_session="fetch")
        session.commit()
        response = test_client.get(
            "/public/dags/example_python_operator/dagRuns/TEST_DAG_RUN_ID/taskInstances/print_the_context"
        )

        assert response.status_code == 200
        assert response.json() == {
            "dag_id": "example_python_operator",
            "duration": 10000.0,
            "end_date": "2020-01-03T00:00:00Z",
            "logical_date": "2020-01-01T00:00:00Z",
            "executor": None,
            "executor_config": "{}",
            "hostname": "",
            "id": mock.ANY,
            "map_index": -1,
            "max_tries": 0,
            "note": "placeholder-note",
            "operator": None,
            "pid": 100,
            "pool": "default_pool",
            "pool_slots": 1,
            "priority_weight": 9,
            "queue": "default_queue",
            "queued_when": None,
            "start_date": "2020-01-02T00:00:00Z",
            "state": "running",
            "task_id": "print_the_context",
            "task_display_name": "print_the_context",
            "try_number": 0,
            "unixname": getuser(),
            "dag_run_id": "TEST_DAG_RUN_ID",
            "rendered_fields": {},
            "rendered_map_index": None,
            "trigger": None,
            "triggerer_job": None,
        }

    def test_should_respond_200_with_task_state_in_deferred(self, test_client, session):
        now = pendulum.now("UTC")
        ti = self.create_task_instances(
            session, task_instances=[{"state": State.DEFERRED}], update_extras=True
        )[0]
        ti.trigger = Trigger("none", {})
        ti.trigger.created_date = now
        ti.triggerer_job = Job()
        TriggererJobRunner(job=ti.triggerer_job)
        ti.triggerer_job.state = "running"
        session.commit()
        response = test_client.get(
            "/public/dags/example_python_operator/dagRuns/TEST_DAG_RUN_ID/taskInstances/print_the_context"
        )
        data = response.json()

        # this logic in effect replicates mock.ANY for these values
        values_to_ignore = {
            "trigger": ["created_date", "id", "triggerer_id"],
            "triggerer_job": ["executor_class", "hostname", "id", "latest_heartbeat", "start_date"],
        }
        for k, v in values_to_ignore.items():
            for elem in v:
                del data[k][elem]

        assert response.status_code == 200
        assert data == {
            "dag_id": "example_python_operator",
            "duration": 10000.0,
            "end_date": "2020-01-03T00:00:00Z",
            "logical_date": "2020-01-01T00:00:00Z",
            "executor": None,
            "executor_config": "{}",
            "hostname": "",
            "id": mock.ANY,
            "map_index": -1,
            "max_tries": 0,
            "note": "placeholder-note",
            "operator": "PythonOperator",
            "pid": 100,
            "pool": "default_pool",
            "pool_slots": 1,
            "priority_weight": 9,
            "queue": "default_queue",
            "queued_when": None,
            "start_date": "2020-01-02T00:00:00Z",
            "state": "deferred",
            "task_id": "print_the_context",
            "task_display_name": "print_the_context",
            "try_number": 0,
            "unixname": getuser(),
            "dag_run_id": "TEST_DAG_RUN_ID",
            "rendered_fields": {},
            "rendered_map_index": None,
            "trigger": {
                "classpath": "none",
                "kwargs": "{}",
            },
            "triggerer_job": {
                "dag_id": None,
                "end_date": None,
                "job_type": "TriggererJob",
                "state": "running",
                "unixname": getuser(),
            },
        }

    def test_should_respond_200_with_task_state_in_removed(self, test_client, session):
        self.create_task_instances(session, task_instances=[{"state": State.REMOVED}], update_extras=True)
        response = test_client.get(
            "/public/dags/example_python_operator/dagRuns/TEST_DAG_RUN_ID/taskInstances/print_the_context"
        )
        assert response.status_code == 200
        assert response.json() == {
            "dag_id": "example_python_operator",
            "duration": 10000.0,
            "end_date": "2020-01-03T00:00:00Z",
            "logical_date": "2020-01-01T00:00:00Z",
            "executor": None,
            "executor_config": "{}",
            "hostname": "",
            "id": mock.ANY,
            "map_index": -1,
            "max_tries": 0,
            "note": "placeholder-note",
            "operator": "PythonOperator",
            "pid": 100,
            "pool": "default_pool",
            "pool_slots": 1,
            "priority_weight": 9,
            "queue": "default_queue",
            "queued_when": None,
            "start_date": "2020-01-02T00:00:00Z",
            "state": "removed",
            "task_id": "print_the_context",
            "task_display_name": "print_the_context",
            "try_number": 0,
            "unixname": getuser(),
            "dag_run_id": "TEST_DAG_RUN_ID",
            "rendered_fields": {},
            "rendered_map_index": None,
            "trigger": None,
            "triggerer_job": None,
        }

    def test_should_respond_200_task_instance_with_rendered(self, test_client, session):
        tis = self.create_task_instances(session)
        session.query()
        rendered_fields = RTIF(tis[0], render_templates=False)
        session.add(rendered_fields)
        session.commit()
        response = test_client.get(
            "/public/dags/example_python_operator/dagRuns/TEST_DAG_RUN_ID/taskInstances/print_the_context"
        )
        assert response.status_code == 200

        assert response.json() == {
            "dag_id": "example_python_operator",
            "duration": 10000.0,
            "end_date": "2020-01-03T00:00:00Z",
            "logical_date": "2020-01-01T00:00:00Z",
            "executor": None,
            "executor_config": "{}",
            "hostname": "",
            "id": mock.ANY,
            "map_index": -1,
            "max_tries": 0,
            "note": "placeholder-note",
            "operator": "PythonOperator",
            "pid": 100,
            "pool": "default_pool",
            "pool_slots": 1,
            "priority_weight": 9,
            "queue": "default_queue",
            "queued_when": None,
            "start_date": "2020-01-02T00:00:00Z",
            "state": "running",
            "task_id": "print_the_context",
            "task_display_name": "print_the_context",
            "try_number": 0,
            "unixname": getuser(),
            "dag_run_id": "TEST_DAG_RUN_ID",
            "rendered_fields": {"op_args": [], "op_kwargs": {}, "templates_dict": None},
            "rendered_map_index": None,
            "trigger": None,
            "triggerer_job": None,
        }

    def test_raises_404_for_nonexistent_task_instance(self, test_client):
        response = test_client.get(
            "/public/dags/example_python_operator/dagRuns/TEST_DAG_RUN_ID/taskInstances/print_the_context"
        )
        assert response.status_code == 404
        assert response.json() == {
            "detail": "The Task Instance with dag_id: `example_python_operator`, run_id: `TEST_DAG_RUN_ID` and task_id: `print_the_context` was not found"
        }

    def test_raises_404_for_mapped_task_instance_with_multiple_indexes(self, test_client, session):
        tis = self.create_task_instances(session)

        old_ti = tis[0]

        for index in range(3):
            ti = TaskInstance(task=old_ti.task, run_id=old_ti.run_id, map_index=index)
            for attr in ["duration", "end_date", "pid", "start_date", "state", "queue", "note"]:
                setattr(ti, attr, getattr(old_ti, attr))
            session.add(ti)
        session.delete(old_ti)
        session.commit()

        response = test_client.get(
            "/public/dags/example_python_operator/dagRuns/TEST_DAG_RUN_ID/taskInstances/print_the_context"
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "Task instance is mapped, add the map_index value to the URL"}

    def test_raises_404_for_mapped_task_instance_with_one_index(self, test_client, session):
        tis = self.create_task_instances(session)

        old_ti = tis[0]

        ti = TaskInstance(task=old_ti.task, run_id=old_ti.run_id, map_index=2)
        for attr in ["duration", "end_date", "pid", "start_date", "state", "queue", "note"]:
            setattr(ti, attr, getattr(old_ti, attr))
        session.add(ti)
        session.delete(old_ti)
        session.commit()

        response = test_client.get(
            "/public/dags/example_python_operator/dagRuns/TEST_DAG_RUN_ID/taskInstances/print_the_context"
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "Task instance is mapped, add the map_index value to the URL"}


class TestGetMappedTaskInstance(TestTaskInstanceEndpoint):
    def test_should_respond_200_mapped_task_instance_with_rtif(self, test_client, session):
        """Verify we don't duplicate rows through join to RTIF"""
        tis = self.create_task_instances(session)
        old_ti = tis[0]
        for idx in (1, 2):
            ti = TaskInstance(task=old_ti.task, run_id=old_ti.run_id, map_index=idx)
            ti.rendered_task_instance_fields = RTIF(ti, render_templates=False)
            for attr in ["duration", "end_date", "pid", "start_date", "state", "queue", "note"]:
                setattr(ti, attr, getattr(old_ti, attr))
            session.add(ti)
        session.commit()

        # in each loop, we should get the right mapped TI back
        for map_index in (1, 2):
            response = test_client.get(
                "/public/dags/example_python_operator/dagRuns/TEST_DAG_RUN_ID/taskInstances"
                f"/print_the_context/{map_index}",
            )
            assert response.status_code == 200

            assert response.json() == {
                "dag_id": "example_python_operator",
                "duration": 10000.0,
                "end_date": "2020-01-03T00:00:00Z",
                "logical_date": "2020-01-01T00:00:00Z",
                "executor": None,
                "executor_config": "{}",
                "hostname": "",
                "id": mock.ANY,
                "map_index": map_index,
                "max_tries": 0,
                "note": "placeholder-note",
                "operator": "PythonOperator",
                "pid": 100,
                "pool": "default_pool",
                "pool_slots": 1,
                "priority_weight": 9,
                "queue": "default_queue",
                "queued_when": None,
                "start_date": "2020-01-02T00:00:00Z",
                "state": "running",
                "task_id": "print_the_context",
                "task_display_name": "print_the_context",
                "try_number": 0,
                "unixname": getuser(),
                "dag_run_id": "TEST_DAG_RUN_ID",
                "rendered_fields": {"op_args": [], "op_kwargs": {}, "templates_dict": None},
                "rendered_map_index": None,
                "trigger": None,
                "triggerer_job": None,
            }

    def test_should_respond_404_wrong_map_index(self, test_client, session):
        self.create_task_instances(session)

        response = test_client.get(
            "/public/dags/example_python_operator/dagRuns/TEST_DAG_RUN_ID/taskInstances"
            "/print_the_context/10",
        )
        assert response.status_code == 404

        assert response.json() == {
            "detail": "The Mapped Task Instance with dag_id: `example_python_operator`, run_id: `TEST_DAG_RUN_ID`, task_id: `print_the_context`, and map_index: `10` was not found"
        }


class TestGetMappedTaskInstances:
    @pytest.fixture(autouse=True)
    def setup_attrs(self) -> None:
        self.default_time = DEFAULT_DATETIME_1
        self.ti_init = {
            "logical_date": self.default_time,
            "state": State.RUNNING,
        }
        self.ti_extras = {
            "start_date": self.default_time + dt.timedelta(days=1),
            "end_date": self.default_time + dt.timedelta(days=2),
            "pid": 100,
            "duration": 10000,
            "pool": "default_pool",
            "queue": "default_queue",
        }
        clear_db_runs()
        clear_rendered_ti_fields()

    def create_dag_runs_with_mapped_tasks(self, dag_maker, session, dags=None):
        for dag_id, dag in (dags or {}).items():
            count = dag["success"] + dag["running"]
            with dag_maker(session=session, dag_id=dag_id, start_date=DEFAULT_DATETIME_1):
                task1 = BaseOperator(task_id="op1")
                mapped = MockOperator.partial(task_id="task_2", executor="default").expand(arg2=task1.output)

            dr = dag_maker.create_dagrun(run_id=f"run_{dag_id}")

            session.add(
                TaskMap(
                    dag_id=dr.dag_id,
                    task_id=task1.task_id,
                    run_id=dr.run_id,
                    map_index=-1,
                    length=count,
                    keys=None,
                )
            )

            if count:
                # Remove the map_index=-1 TI when we're creating other TIs
                session.query(TaskInstance).filter(
                    TaskInstance.dag_id == mapped.dag_id,
                    TaskInstance.task_id == mapped.task_id,
                    TaskInstance.run_id == dr.run_id,
                ).delete()

            for index, state in enumerate(
                itertools.chain(
                    itertools.repeat(TaskInstanceState.SUCCESS, dag["success"]),
                    itertools.repeat(TaskInstanceState.FAILED, dag["failed"]),
                    itertools.repeat(TaskInstanceState.RUNNING, dag["running"]),
                )
            ):
                ti = TaskInstance(mapped, run_id=dr.run_id, map_index=index, state=state)
                setattr(ti, "start_date", DEFAULT_DATETIME_1)
                session.add(ti)

            dagbag = DagBag(os.devnull, include_examples=False)
            dagbag.dags = {dag_id: dag_maker.dag}
            dagbag.sync_to_db()
            session.flush()

            mapped.expand_mapped_task(dr.run_id, session=session)

    @pytest.fixture
    def one_task_with_mapped_tis(self, dag_maker, session):
        self.create_dag_runs_with_mapped_tasks(
            dag_maker,
            session,
            dags={
                "mapped_tis": {
                    "success": 3,
                    "failed": 0,
                    "running": 0,
                },
            },
        )

    @pytest.fixture
    def one_task_with_single_mapped_ti(self, dag_maker, session):
        self.create_dag_runs_with_mapped_tasks(
            dag_maker,
            session,
            dags={
                "mapped_tis": {
                    "success": 1,
                    "failed": 0,
                    "running": 0,
                },
            },
        )

    @pytest.fixture
    def one_task_with_many_mapped_tis(self, dag_maker, session):
        self.create_dag_runs_with_mapped_tasks(
            dag_maker,
            session,
            dags={
                "mapped_tis": {
                    "success": 5,
                    "failed": 20,
                    "running": 85,
                },
            },
        )

    @pytest.fixture
    def one_task_with_zero_mapped_tis(self, dag_maker, session):
        self.create_dag_runs_with_mapped_tasks(
            dag_maker,
            session,
            dags={
                "mapped_tis": {
                    "success": 0,
                    "failed": 0,
                    "running": 0,
                },
            },
        )

    def test_should_respond_404(self, test_client):
        response = test_client.get(
            "/public/dags/mapped_tis/dagRuns/run_mapped_tis/taskInstances/task_2/listMapped",
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "DAG mapped_tis not found"}

    def test_should_respond_200(self, one_task_with_many_mapped_tis, test_client):
        response = test_client.get(
            "/public/dags/mapped_tis/dagRuns/run_mapped_tis/taskInstances/task_2/listMapped",
        )

        assert response.status_code == 200
        assert response.json()["total_entries"] == 110
        assert len(response.json()["task_instances"]) == 100

    def test_offset_limit(self, test_client, one_task_with_many_mapped_tis):
        response = test_client.get(
            "/public/dags/mapped_tis/dagRuns/run_mapped_tis/taskInstances/task_2/listMapped",
            params={"offset": 4, "limit": 10},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total_entries"] == 110
        assert len(body["task_instances"]) == 10
        assert list(range(4, 14)) == [ti["map_index"] for ti in body["task_instances"]]

    def test_order(self, test_client, one_task_with_many_mapped_tis):
        response = test_client.get(
            "/public/dags/mapped_tis/dagRuns/run_mapped_tis/taskInstances/task_2/listMapped",
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total_entries"] == 110
        assert len(body["task_instances"]) == 100
        assert list(range(100)) == [ti["map_index"] for ti in body["task_instances"]]

    def test_mapped_task_instances_reverse_order(self, test_client, one_task_with_many_mapped_tis):
        response = test_client.get(
            "/public/dags/mapped_tis/dagRuns/run_mapped_tis/taskInstances/task_2/listMapped",
            params={"order_by": "-map_index"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total_entries"] == 110
        assert len(body["task_instances"]) == 100
        assert list(range(109, 9, -1)) == [ti["map_index"] for ti in body["task_instances"]]

    def test_state_order(self, test_client, one_task_with_many_mapped_tis):
        response = test_client.get(
            "/public/dags/mapped_tis/dagRuns/run_mapped_tis/taskInstances/task_2/listMapped",
            params={"order_by": "-state"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total_entries"] == 110
        assert len(body["task_instances"]) == 100
        assert list(range(5)[::-1]) + list(range(25, 110)[::-1]) + list(range(15, 25)[::-1]) == [
            ti["map_index"] for ti in body["task_instances"]
        ]
        # State ascending
        response = test_client.get(
            "/public/dags/mapped_tis/dagRuns/run_mapped_tis/taskInstances/task_2/listMapped",
            params={"order_by": "state", "limit": 108},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total_entries"] == 110
        assert len(body["task_instances"]) == 108
        assert list(range(5, 25)) + list(range(25, 110)) + list(range(3)) == [
            ti["map_index"] for ti in body["task_instances"]
        ]

    def test_rendered_map_index_order(self, test_client, session, one_task_with_many_mapped_tis):
        ti = (
            session.query(TaskInstance)
            .where(TaskInstance.task_id == "task_2", TaskInstance.map_index == 0)
            .first()
        )

        ti.rendered_map_index = "a"

        session.commit()

        response = test_client.get(
            "/public/dags/mapped_tis/dagRuns/run_mapped_tis/taskInstances/task_2/listMapped",
            params={"order_by": "-rendered_map_index"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total_entries"] == 110
        assert len(body["task_instances"]) == 100
        assert [0] + list(range(11, 110)[::-1]) == [ti["map_index"] for ti in body["task_instances"]]
        # State ascending
        response = test_client.get(
            "/public/dags/mapped_tis/dagRuns/run_mapped_tis/taskInstances/task_2/listMapped",
            params={"order_by": "rendered_map_index", "limit": 108},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total_entries"] == 110
        assert len(body["task_instances"]) == 108
        assert [0] + list(range(1, 108)) == [ti["map_index"] for ti in body["task_instances"]]

    def test_with_date(self, test_client, one_task_with_mapped_tis):
        response = test_client.get(
            "/public/dags/mapped_tis/dagRuns/run_mapped_tis/taskInstances/task_2/listMapped",
            params={"start_date_gte": DEFAULT_DATETIME_1},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total_entries"] == 3
        assert len(body["task_instances"]) == 3

        response = test_client.get(
            "/public/dags/mapped_tis/dagRuns/run_mapped_tis/taskInstances/task_2/listMapped",
            params={"start_date_gte": DEFAULT_DATETIME_2},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total_entries"] == 0
        assert body["task_instances"] == []

    def test_with_logical_date(self, test_client, one_task_with_mapped_tis):
        response = test_client.get(
            "/public/dags/mapped_tis/dagRuns/run_mapped_tis/taskInstances/task_2/listMapped",
            params={"logical_date_gte": DEFAULT_DATETIME_1},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total_entries"] == 3
        assert len(body["task_instances"]) == 3

        response = test_client.get(
            "/public/dags/mapped_tis/dagRuns/run_mapped_tis/taskInstances/task_2/listMapped",
            params={"logical_date_gte": DEFAULT_DATETIME_2},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total_entries"] == 0
        assert body["task_instances"] == []

    @pytest.mark.parametrize(
        "query_params, expected_total_entries, expected_task_instance_count",
        [
            ({"state": "success"}, 3, 3),
            ({"state": "running"}, 0, 0),
            ({"pool": "default_pool"}, 3, 3),
            ({"pool": "test_pool"}, 0, 0),
            ({"queue": "default"}, 3, 3),
            ({"queue": "test_queue"}, 0, 0),
            ({"executor": "default"}, 3, 3),
            ({"executor": "no_exec"}, 0, 0),
        ],
    )
    def test_mapped_task_instances_filters(
        self,
        test_client,
        one_task_with_mapped_tis,
        query_params,
        expected_total_entries,
        expected_task_instance_count,
    ):
        response = test_client.get(
            "/public/dags/mapped_tis/dagRuns/run_mapped_tis/taskInstances/task_2/listMapped",
            params=query_params,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total_entries"] == expected_total_entries
        assert len(body["task_instances"]) == expected_task_instance_count

    def test_with_zero_mapped(self, test_client, one_task_with_zero_mapped_tis, session):
        response = test_client.get(
            "/public/dags/mapped_tis/dagRuns/run_mapped_tis/taskInstances/task_2/listMapped",
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total_entries"] == 0
        assert body["task_instances"] == []

    def test_should_raise_404_not_found_for_nonexistent_task(
        self, one_task_with_zero_mapped_tis, test_client
    ):
        response = test_client.get(
            "/public/dags/mapped_tis/dagRuns/run_mapped_tis/taskInstances/nonexistent_task/listMapped",
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "Task id nonexistent_task not found"


class TestGetTaskInstances(TestTaskInstanceEndpoint):
    @pytest.mark.parametrize(
        "task_instances, update_extras, url, params, expected_ti",
        [
            pytest.param(
                [
                    {"logical_date": DEFAULT_DATETIME_1},
                    {"logical_date": DEFAULT_DATETIME_1 + dt.timedelta(days=1)},
                    {"logical_date": DEFAULT_DATETIME_1 + dt.timedelta(days=2)},
                ],
                False,
                "/public/dags/example_python_operator/dagRuns/~/taskInstances",
                {"logical_date_lte": DEFAULT_DATETIME_1},
                1,
                id="test logical date filter",
            ),
            pytest.param(
                [
                    {"start_date": DEFAULT_DATETIME_1},
                    {"start_date": DEFAULT_DATETIME_1 + dt.timedelta(days=1)},
                    {"start_date": DEFAULT_DATETIME_1 + dt.timedelta(days=2)},
                ],
                True,
                "/public/dags/example_python_operator/dagRuns/~/taskInstances",
                {"start_date_gte": DEFAULT_DATETIME_1, "start_date_lte": DEFAULT_DATETIME_STR_2},
                2,
                id="test start date filter",
            ),
            pytest.param(
                [
                    {"end_date": DEFAULT_DATETIME_1},
                    {"end_date": DEFAULT_DATETIME_1 + dt.timedelta(days=1)},
                    {"end_date": DEFAULT_DATETIME_1 + dt.timedelta(days=2)},
                ],
                True,
                "/public/dags/example_python_operator/dagRuns/~/taskInstances?",
                {"end_date_gte": DEFAULT_DATETIME_1, "end_date_lte": DEFAULT_DATETIME_STR_2},
                2,
                id="test end date filter",
            ),
            pytest.param(
                [
                    {"duration": 100},
                    {"duration": 150},
                    {"duration": 200},
                ],
                True,
                "/public/dags/example_python_operator/dagRuns/TEST_DAG_RUN_ID/taskInstances",
                {"duration_gte": 100, "duration_lte": 200},
                3,
                id="test duration filter",
            ),
            pytest.param(
                [
                    {"duration": 100},
                    {"duration": 150},
                    {"duration": 200},
                ],
                True,
                "/public/dags/~/dagRuns/~/taskInstances",
                {"duration_gte": 100, "duration_lte": 200},
                3,
                id="test duration filter ~",
            ),
            pytest.param(
                [
                    {"state": State.RUNNING},
                    {"state": State.QUEUED},
                    {"state": State.SUCCESS},
                    {"state": State.NONE},
                ],
                False,
                ("/public/dags/example_python_operator/dagRuns/TEST_DAG_RUN_ID/taskInstances"),
                {"state": ["running", "queued", "none"]},
                3,
                id="test state filter",
            ),
            pytest.param(
                [
                    {"state": State.NONE},
                    {"state": State.NONE},
                    {"state": State.NONE},
                    {"state": State.NONE},
                ],
                False,
                ("/public/dags/example_python_operator/dagRuns/TEST_DAG_RUN_ID/taskInstances"),
                {},
                4,
                id="test null states with no filter",
            ),
            pytest.param(
                [
                    {"pool": "test_pool_1"},
                    {"pool": "test_pool_2"},
                    {"pool": "test_pool_3"},
                ],
                True,
                ("/public/dags/example_python_operator/dagRuns/TEST_DAG_RUN_ID/taskInstances"),
                {"pool": ["test_pool_1", "test_pool_2"]},
                2,
                id="test pool filter",
            ),
            pytest.param(
                [
                    {"pool": "test_pool_1"},
                    {"pool": "test_pool_2"},
                    {"pool": "test_pool_3"},
                ],
                True,
                "/public/dags/~/dagRuns/~/taskInstances",
                {"pool": ["test_pool_1", "test_pool_2"]},
                2,
                id="test pool filter ~",
            ),
            pytest.param(
                [
                    {"queue": "test_queue_1"},
                    {"queue": "test_queue_2"},
                    {"queue": "test_queue_3"},
                ],
                True,
                "/public/dags/example_python_operator/dagRuns/TEST_DAG_RUN_ID/taskInstances",
                {"queue": ["test_queue_1", "test_queue_2"]},
                2,
                id="test queue filter",
            ),
            pytest.param(
                [
                    {"queue": "test_queue_1"},
                    {"queue": "test_queue_2"},
                    {"queue": "test_queue_3"},
                ],
                True,
                "/public/dags/~/dagRuns/~/taskInstances",
                {"queue": ["test_queue_1", "test_queue_2"]},
                2,
                id="test queue filter ~",
            ),
            pytest.param(
                [
                    {"executor": "test_exec_1"},
                    {"executor": "test_exec_2"},
                    {"executor": "test_exec_3"},
                ],
                True,
                ("/public/dags/example_python_operator/dagRuns/TEST_DAG_RUN_ID/taskInstances"),
                {"executor": ["test_exec_1", "test_exec_2"]},
                2,
                id="test_executor_filter",
            ),
            pytest.param(
                [
                    {"executor": "test_exec_1"},
                    {"executor": "test_exec_2"},
                    {"executor": "test_exec_3"},
                ],
                True,
                "/public/dags/~/dagRuns/~/taskInstances",
                {"executor": ["test_exec_1", "test_exec_2"]},
                2,
                id="test executor filter ~",
            ),
        ],
    )
    def test_should_respond_200(
        self, test_client, task_instances, update_extras, url, params, expected_ti, session
    ):
        self.create_task_instances(
            session,
            update_extras=update_extras,
            task_instances=task_instances,
        )
        response = test_client.get(url, params=params)
        assert response.status_code == 200
        assert response.json()["total_entries"] == expected_ti
        assert len(response.json()["task_instances"]) == expected_ti

    @pytest.mark.xfail(reason="permissions not implemented yet.")
    def test_return_TI_only_from_readable_dags(self, test_client, session):
        task_instances = {
            "example_python_operator": 1,
            "example_skip_dag": 2,
        }
        for dag_id in task_instances:
            self.create_task_instances(
                session,
                task_instances=[
                    {"logical_date": DEFAULT_DATETIME_1 + dt.timedelta(days=i)}
                    for i in range(task_instances[dag_id])
                ],
                dag_id=dag_id,
            )
        response = test_client.get("/public/dags/~/dagRuns/~/taskInstances")
        assert response.status_code == 200
        assert response.json["total_entries"] == 3
        assert len(response.json["task_instances"]) == 3

    def test_should_respond_200_for_dag_id_filter(self, test_client, session):
        self.create_task_instances(session)
        self.create_task_instances(session, dag_id="example_skip_dag")
        response = test_client.get(
            "/public/dags/example_python_operator/dagRuns/~/taskInstances",
        )

        assert response.status_code == 200
        count = session.query(TaskInstance).filter(TaskInstance.dag_id == "example_python_operator").count()
        assert count == response.json()["total_entries"]
        assert count == len(response.json()["task_instances"])

    def test_should_respond_200_for_order_by(self, test_client, session):
        dag_id = "example_python_operator"
        self.create_task_instances(
            session,
            task_instances=[
                {"start_date": DEFAULT_DATETIME_1 + dt.timedelta(minutes=(i + 1))} for i in range(10)
            ],
            dag_id=dag_id,
        )

        ti_count = session.query(TaskInstance).filter(TaskInstance.dag_id == dag_id).count()

        # Ascending order
        response_asc = test_client.get(
            "/public/dags/~/dagRuns/~/taskInstances", params={"order_by": "start_date"}
        )
        assert response_asc.status_code == 200
        assert response_asc.json()["total_entries"] == ti_count
        assert len(response_asc.json()["task_instances"]) == ti_count

        # Descending order
        response_desc = test_client.get(
            "/public/dags/~/dagRuns/~/taskInstances", params={"order_by": "-start_date"}
        )
        assert response_desc.status_code == 200
        assert response_desc.json()["total_entries"] == ti_count
        assert len(response_desc.json()["task_instances"]) == ti_count

        # Compare
        start_dates_asc = [ti["start_date"] for ti in response_asc.json()["task_instances"]]
        assert len(start_dates_asc) == ti_count
        start_dates_desc = [ti["start_date"] for ti in response_desc.json()["task_instances"]]
        assert len(start_dates_desc) == ti_count
        assert start_dates_asc == list(reversed(start_dates_desc))

    def test_should_respond_200_for_pagination(self, test_client, session):
        dag_id = "example_python_operator"
        self.create_task_instances(
            session,
            task_instances=[
                {"start_date": DEFAULT_DATETIME_1 + dt.timedelta(minutes=(i + 1))} for i in range(10)
            ],
            dag_id=dag_id,
        )

        # First 5 items
        response_batch1 = test_client.get(
            "/public/dags/~/dagRuns/~/taskInstances", params={"limit": 5, "offset": 0, "dag_ids": [dag_id]}
        )
        assert response_batch1.status_code == 200, response_batch1.json()
        num_entries_batch1 = len(response_batch1.json()["task_instances"])
        assert num_entries_batch1 == 5
        assert len(response_batch1.json()["task_instances"]) == 5

        # 5 items after that
        response_batch2 = test_client.get(
            "/public/dags/~/dagRuns/~/taskInstances", params={"limit": 5, "offset": 5, "dag_ids": [dag_id]}
        )
        assert response_batch2.status_code == 200, response_batch2.json()
        num_entries_batch2 = len(response_batch2.json()["task_instances"])
        assert num_entries_batch2 > 0
        assert len(response_batch2.json()["task_instances"]) > 0

        # Match
        ti_count = session.query(TaskInstance).filter(TaskInstance.dag_id == dag_id).count()
        assert response_batch1.json()["total_entries"] == response_batch2.json()["total_entries"] == ti_count
        assert (num_entries_batch1 + num_entries_batch2) == ti_count
        assert response_batch1 != response_batch2


class TestGetTaskDependencies(TestTaskInstanceEndpoint):
    def setup_method(self):
        clear_db_runs()

    def teardown_method(self):
        clear_db_runs()

    def test_should_respond_empty_non_scheduled(self, test_client, session):
        self.create_task_instances(session)
        response = test_client.get(
            "/public/dags/example_python_operator/dagRuns/TEST_DAG_RUN_ID/taskInstances/"
            "print_the_context/dependencies",
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"dependencies": []}

    @pytest.mark.parametrize(
        "state, dependencies",
        [
            (
                State.SCHEDULED,
                {
                    "dependencies": [
                        {
                            "name": "Logical Date",
                            "reason": "The logical date is 2020-01-01T00:00:00+00:00 but this is "
                            "before the task's start date 2021-01-01T00:00:00+00:00.",
                        },
                        {
                            "name": "Logical Date",
                            "reason": "The logical date is 2020-01-01T00:00:00+00:00 but this is "
                            "before the task's DAG's start date 2021-01-01T00:00:00+00:00.",
                        },
                    ],
                },
            ),
            (
                State.NONE,
                {
                    "dependencies": [
                        {
                            "name": "Logical Date",
                            "reason": "The logical date is 2020-01-01T00:00:00+00:00 but this is before the task's start date 2021-01-01T00:00:00+00:00.",
                        },
                        {
                            "name": "Logical Date",
                            "reason": "The logical date is 2020-01-01T00:00:00+00:00 but this is before the task's DAG's start date 2021-01-01T00:00:00+00:00.",
                        },
                        {"name": "Task Instance State", "reason": "Task is in the 'None' state."},
                    ]
                },
            ),
        ],
    )
    def test_should_respond_dependencies(self, test_client, session, state, dependencies):
        self.create_task_instances(session, task_instances=[{"state": state}], update_extras=True)

        response = test_client.get(
            "/public/dags/example_python_operator/dagRuns/TEST_DAG_RUN_ID/taskInstances/"
            "print_the_context/dependencies",
        )
        assert response.status_code == 200, response.text
        assert response.json() == dependencies

    def test_should_respond_dependencies_mapped(self, test_client, session):
        tis = self.create_task_instances(
            session, task_instances=[{"state": State.SCHEDULED}], update_extras=True
        )
        old_ti = tis[0]

        ti = TaskInstance(task=old_ti.task, run_id=old_ti.run_id, map_index=0, state=old_ti.state)
        session.add(ti)
        session.commit()

        response = test_client.get(
            "/public/dags/example_python_operator/dagRuns/TEST_DAG_RUN_ID/taskInstances/"
            "print_the_context/0/dependencies",
        )
        assert response.status_code == 200, response.text


class TestGetTaskInstancesBatch(TestTaskInstanceEndpoint):
    @pytest.mark.parametrize(
        "task_instances, update_extras, payload, expected_ti_count",
        [
            pytest.param(
                [
                    {"queue": "test_queue_1"},
                    {"queue": "test_queue_2"},
                    {"queue": "test_queue_3"},
                ],
                True,
                {"queue": ["test_queue_1", "test_queue_2"]},
                2,
                id="test queue filter",
            ),
            pytest.param(
                [
                    {"executor": "test_exec_1"},
                    {"executor": "test_exec_2"},
                    {"executor": "test_exec_3"},
                ],
                True,
                {"executor": ["test_exec_1", "test_exec_2"]},
                2,
                id="test executor filter",
            ),
            pytest.param(
                [
                    {"duration": 100},
                    {"duration": 150},
                    {"duration": 200},
                ],
                True,
                {"duration_gte": 100, "duration_lte": 200},
                3,
                id="test duration filter",
            ),
            pytest.param(
                [
                    {"logical_date": DEFAULT_DATETIME_1},
                    {"logical_date": DEFAULT_DATETIME_1 + dt.timedelta(days=1)},
                    {"logical_date": DEFAULT_DATETIME_1 + dt.timedelta(days=2)},
                    {"logical_date": DEFAULT_DATETIME_1 + dt.timedelta(days=3)},
                    {"logical_date": DEFAULT_DATETIME_1 + dt.timedelta(days=4)},
                    {"logical_date": DEFAULT_DATETIME_1 + dt.timedelta(days=5)},
                ],
                False,
                {
                    "logical_date_gte": DEFAULT_DATETIME_1.isoformat(),
                    "logical_date_lte": (DEFAULT_DATETIME_1 + dt.timedelta(days=2)).isoformat(),
                },
                3,
                id="with logical date filter",
            ),
            pytest.param(
                [
                    {"logical_date": DEFAULT_DATETIME_1},
                    {"logical_date": DEFAULT_DATETIME_1 + dt.timedelta(days=1)},
                    {"logical_date": DEFAULT_DATETIME_1 + dt.timedelta(days=2)},
                    {"logical_date": DEFAULT_DATETIME_1 + dt.timedelta(days=3)},
                ],
                False,
                {
                    "dag_run_ids": ["TEST_DAG_RUN_ID_0", "TEST_DAG_RUN_ID_1"],
                },
                2,
                id="test dag run id filter",
            ),
            pytest.param(
                [
                    {"logical_date": DEFAULT_DATETIME_1},
                    {"logical_date": DEFAULT_DATETIME_1 + dt.timedelta(days=1)},
                    {"logical_date": DEFAULT_DATETIME_1 + dt.timedelta(days=2)},
                    {"logical_date": DEFAULT_DATETIME_1 + dt.timedelta(days=3)},
                ],
                False,
                {
                    "task_ids": ["print_the_context", "log_sql_query"],
                },
                2,
                id="test task id filter",
            ),
        ],
    )
    def test_should_respond_200(
        self, test_client, task_instances, update_extras, payload, expected_ti_count, session
    ):
        self.create_task_instances(
            session,
            update_extras=update_extras,
            task_instances=task_instances,
        )
        response = test_client.post(
            "/public/dags/~/dagRuns/~/taskInstances/list",
            json=payload,
        )
        body = response.json()
        assert response.status_code == 200, body
        assert expected_ti_count == body["total_entries"]
        assert expected_ti_count == len(body["task_instances"])

    def test_should_respond_200_for_order_by(self, test_client, session):
        dag_id = "example_python_operator"
        self.create_task_instances(
            session,
            task_instances=[
                {"start_date": DEFAULT_DATETIME_1 + dt.timedelta(minutes=(i + 1))} for i in range(10)
            ],
            dag_id=dag_id,
        )

        ti_count = session.query(TaskInstance).filter(TaskInstance.dag_id == dag_id).count()

        # Ascending order
        response_asc = test_client.post(
            "/public/dags/~/dagRuns/~/taskInstances/list",
            json={"order_by": "start_date", "dag_ids": [dag_id]},
        )
        assert response_asc.status_code == 200, response_asc.json()
        assert response_asc.json()["total_entries"] == ti_count
        assert len(response_asc.json()["task_instances"]) == ti_count

        # Descending order
        response_desc = test_client.post(
            "/public/dags/~/dagRuns/~/taskInstances/list",
            json={"order_by": "-start_date", "dag_ids": [dag_id]},
        )
        assert response_desc.status_code == 200, response_desc.json()
        assert response_desc.json()["total_entries"] == ti_count
        assert len(response_desc.json()["task_instances"]) == ti_count

        # Compare
        start_dates_asc = [ti["start_date"] for ti in response_asc.json()["task_instances"]]
        assert len(start_dates_asc) == ti_count
        start_dates_desc = [ti["start_date"] for ti in response_desc.json()["task_instances"]]
        assert len(start_dates_desc) == ti_count
        assert start_dates_asc == list(reversed(start_dates_desc))

    @pytest.mark.parametrize(
        "task_instances, payload, expected_ti_count",
        [
            pytest.param(
                [
                    {"task": "test_1"},
                    {"task": "test_2"},
                ],
                {"dag_ids": ["latest_only"]},
                2,
                id="task_instance properties",
            ),
        ],
    )
    def test_should_respond_200_when_task_instance_properties_are_none(
        self, test_client, task_instances, payload, expected_ti_count, session
    ):
        self.ti_extras.update(
            {
                "start_date": None,
                "end_date": None,
                "state": None,
            }
        )
        self.create_task_instances(
            session,
            dag_id="latest_only",
            task_instances=task_instances,
        )
        response = test_client.post(
            "/public/dags/~/dagRuns/~/taskInstances/list",
            json=payload,
        )
        body = response.json()
        assert response.status_code == 200, body
        assert expected_ti_count == body["total_entries"]
        assert expected_ti_count == len(body["task_instances"])

    @pytest.mark.parametrize(
        "payload, expected_ti, total_ti",
        [
            pytest.param(
                {"dag_ids": ["example_python_operator", "example_skip_dag"]},
                17,
                17,
                id="with dag filter",
            ),
        ],
    )
    def test_should_respond_200_dag_ids_filter(self, test_client, payload, expected_ti, total_ti, session):
        self.create_task_instances(session)
        self.create_task_instances(session, dag_id="example_skip_dag")
        response = test_client.post(
            "/public/dags/~/dagRuns/~/taskInstances/list",
            json=payload,
        )
        assert response.status_code == 200
        assert len(response.json()["task_instances"]) == expected_ti
        assert response.json()["total_entries"] == total_ti

    def test_should_raise_400_for_no_json(self, test_client):
        response = test_client.post(
            "/public/dags/~/dagRuns/~/taskInstances/list",
        )
        assert response.status_code == 422
        assert response.json()["detail"] == [
            {
                "input": None,
                "loc": ["body"],
                "msg": "Field required",
                "type": "missing",
            },
        ]

    def test_should_respond_422_for_non_wildcard_path_parameters(self, test_client):
        response = test_client.post(
            "/public/dags/non_wildcard/dagRuns/~/taskInstances/list",
        )
        assert response.status_code == 422
        assert "Input should be '~'" in str(response.json()["detail"])

        response = test_client.post(
            "/public/dags/~/dagRuns/non_wildcard/taskInstances/list",
        )
        assert response.status_code == 422
        assert "Input should be '~'" in str(response.json()["detail"])

    @pytest.mark.parametrize(
        "payload, expected",
        [
            ({"end_date_lte": "2020-11-10T12:42:39.442973"}, "Input should have timezone info"),
            ({"end_date_gte": "2020-11-10T12:42:39.442973"}, "Input should have timezone info"),
            ({"start_date_lte": "2020-11-10T12:42:39.442973"}, "Input should have timezone info"),
            ({"start_date_gte": "2020-11-10T12:42:39.442973"}, "Input should have timezone info"),
            ({"logical_date_gte": "2020-11-10T12:42:39.442973"}, "Input should have timezone info"),
            ({"logical_date_lte": "2020-11-10T12:42:39.442973"}, "Input should have timezone info"),
        ],
    )
    def test_should_raise_400_for_naive_and_bad_datetime(self, test_client, payload, expected, session):
        self.create_task_instances(session)
        response = test_client.post(
            "/public/dags/~/dagRuns/~/taskInstances/list",
            json=payload,
        )
        assert response.status_code == 422
        assert expected in str(response.json()["detail"])

    def test_should_respond_200_for_pagination(self, test_client, session):
        dag_id = "example_python_operator"

        self.create_task_instances(
            session,
            task_instances=[
                {"start_date": DEFAULT_DATETIME_1 + dt.timedelta(minutes=(i + 1))} for i in range(10)
            ],
            dag_id=dag_id,
        )

        # First 5 items
        response_batch1 = test_client.post(
            "/public/dags/~/dagRuns/~/taskInstances/list",
            json={"page_limit": 5, "page_offset": 0},
        )
        assert response_batch1.status_code == 200, response_batch1.json()
        num_entries_batch1 = len(response_batch1.json()["task_instances"])
        assert num_entries_batch1 == 5
        assert len(response_batch1.json()["task_instances"]) == 5

        # 5 items after that
        response_batch2 = test_client.post(
            "/public/dags/~/dagRuns/~/taskInstances/list",
            json={"page_limit": 5, "page_offset": 5},
        )
        assert response_batch2.status_code == 200, response_batch2.json()
        num_entries_batch2 = len(response_batch2.json()["task_instances"])
        assert num_entries_batch2 > 0
        assert len(response_batch2.json()["task_instances"]) > 0

        # Match
        ti_count = 9
        assert response_batch1.json()["total_entries"] == response_batch2.json()["total_entries"] == ti_count
        assert (num_entries_batch1 + num_entries_batch2) == ti_count
        assert response_batch1 != response_batch2

        # default limit and offset
        response_batch3 = test_client.post(
            "/public/dags/~/dagRuns/~/taskInstances/list",
            json={},
        )

        num_entries_batch3 = len(response_batch3.json()["task_instances"])
        assert num_entries_batch3 == ti_count
        assert len(response_batch3.json()["task_instances"]) == ti_count


class TestGetTaskInstanceTry(TestTaskInstanceEndpoint):
    def test_should_respond_200(self, test_client, session):
        self.create_task_instances(session, task_instances=[{"state": State.SUCCESS}], with_ti_history=True)
        response = test_client.get(
            "/public/dags/example_python_operator/dagRuns/TEST_DAG_RUN_ID/taskInstances/print_the_context/tries/1"
        )
        assert response.status_code == 200
        assert response.json() == {
            "dag_id": "example_python_operator",
            "duration": 10000.0,
            "end_date": "2020-01-03T00:00:00Z",
            "executor": None,
            "executor_config": "{}",
            "hostname": "",
            "map_index": -1,
            "max_tries": 0,
            "operator": "PythonOperator",
            "pid": 100,
            "pool": "default_pool",
            "pool_slots": 1,
            "priority_weight": 9,
            "queue": "default_queue",
            "queued_when": None,
            "start_date": "2020-01-02T00:00:00Z",
            "state": "success",
            "task_id": "print_the_context",
            "task_display_name": "print_the_context",
            "try_number": 1,
            "unixname": getuser(),
            "dag_run_id": "TEST_DAG_RUN_ID",
        }

    @pytest.mark.parametrize("try_number", [1, 2])
    def test_should_respond_200_with_different_try_numbers(self, test_client, try_number, session):
        self.create_task_instances(session, task_instances=[{"state": State.SUCCESS}], with_ti_history=True)
        response = test_client.get(
            f"/public/dags/example_python_operator/dagRuns/TEST_DAG_RUN_ID/taskInstances/print_the_context/tries/{try_number}",
        )

        assert response.status_code == 200
        assert response.json() == {
            "dag_id": "example_python_operator",
            "duration": 10000.0,
            "end_date": "2020-01-03T00:00:00Z",
            "executor": None,
            "executor_config": "{}",
            "hostname": "",
            "map_index": -1,
            "max_tries": 0 if try_number == 1 else 1,
            "operator": "PythonOperator",
            "pid": 100,
            "pool": "default_pool",
            "pool_slots": 1,
            "priority_weight": 9,
            "queue": "default_queue",
            "queued_when": None,
            "start_date": "2020-01-02T00:00:00Z",
            "state": "success" if try_number == 1 else None,
            "task_id": "print_the_context",
            "task_display_name": "print_the_context",
            "try_number": try_number,
            "unixname": getuser(),
            "dag_run_id": "TEST_DAG_RUN_ID",
        }

    def test_should_respond_200_with_task_state_in_deferred(self, test_client, session):
        now = pendulum.now("UTC")
        ti = self.create_task_instances(
            session,
            task_instances=[{"state": State.DEFERRED}],
            update_extras=True,
        )[0]
        ti.trigger = Trigger("none", {})
        ti.trigger.created_date = now
        ti.triggerer_job = Job()
        TriggererJobRunner(job=ti.triggerer_job)
        ti.triggerer_job.state = "running"
        ti.try_number = 1
        session.merge(ti)
        session.flush()
        # Record the TaskInstanceHistory
        TaskInstanceHistory.record_ti(ti, session=session)
        session.flush()
        # Change TaskInstance try_number to 2, ensuring api checks TIHistory
        ti = session.query(TaskInstance).one_or_none()
        ti.try_number = 2
        session.merge(ti)
        # Set duration and end_date in TaskInstanceHistory for easy testing
        tih = session.query(TaskInstanceHistory).all()[0]
        tih.duration = 10000
        tih.end_date = self.default_time + dt.timedelta(days=2)
        session.merge(tih)
        session.commit()
        # Get the task instance details from TIHistory:
        response = test_client.get(
            "/public/dags/example_python_operator/dagRuns/TEST_DAG_RUN_ID/taskInstances/print_the_context/tries/1",
        )
        assert response.status_code == 200
        data = response.json()

        assert data == {
            "dag_id": "example_python_operator",
            "duration": 10000.0,
            "end_date": "2020-01-03T00:00:00Z",
            "executor": None,
            "executor_config": "{}",
            "hostname": "",
            "map_index": -1,
            "max_tries": 0,
            "operator": "PythonOperator",
            "pid": 100,
            "pool": "default_pool",
            "pool_slots": 1,
            "priority_weight": 9,
            "queue": "default_queue",
            "queued_when": None,
            "start_date": "2020-01-02T00:00:00Z",
            "state": "failed",
            "task_id": "print_the_context",
            "task_display_name": "print_the_context",
            "try_number": 1,
            "unixname": getuser(),
            "dag_run_id": "TEST_DAG_RUN_ID",
        }

    def test_should_respond_200_with_task_state_in_removed(self, test_client, session):
        self.create_task_instances(
            session, task_instances=[{"state": State.REMOVED}], update_extras=True, with_ti_history=True
        )
        response = test_client.get(
            "/public/dags/example_python_operator/dagRuns/TEST_DAG_RUN_ID/taskInstances/print_the_context/tries/1",
        )
        assert response.status_code == 200

        assert response.json() == {
            "dag_id": "example_python_operator",
            "duration": 10000.0,
            "end_date": "2020-01-03T00:00:00Z",
            "executor": None,
            "executor_config": "{}",
            "hostname": "",
            "map_index": -1,
            "max_tries": 0,
            "operator": "PythonOperator",
            "pid": 100,
            "pool": "default_pool",
            "pool_slots": 1,
            "priority_weight": 9,
            "queue": "default_queue",
            "queued_when": None,
            "start_date": "2020-01-02T00:00:00Z",
            "state": "removed",
            "task_id": "print_the_context",
            "task_display_name": "print_the_context",
            "try_number": 1,
            "unixname": getuser(),
            "dag_run_id": "TEST_DAG_RUN_ID",
        }

    def test_raises_404_for_nonexistent_task_instance(self, test_client, session):
        self.create_task_instances(session)
        response = test_client.get(
            "/public/dags/example_python_operator/dagRuns/TEST_DAG_RUN_ID/taskInstances/nonexistent_task/tries/0"
        )
        assert response.status_code == 404

        assert response.json() == {
            "detail": "The Task Instance with dag_id: `example_python_operator`, run_id: `TEST_DAG_RUN_ID`, task_id: `nonexistent_task`, try_number: `0` and map_index: `-1` was not found"
        }