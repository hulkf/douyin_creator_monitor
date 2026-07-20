import argparse
import contextlib
import importlib.util
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_creator_pipeline.py"
SPEC = importlib.util.spec_from_file_location("run_creator_pipeline", MODULE_PATH)
PIPELINE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(PIPELINE)


class PipelineHelpersTest(unittest.TestCase):
    def test_feishu_only_status_ignores_reported_backup_failures(self):
        stages = {
            "feishu_written_back": "success",
            "backup_statuses_written_back": "success",
            "ima_backed_up": "failed",
            "kuake_backed_up": "skipped",
            "obsidian_exported": "skipped",
        }
        self.assertFalse(PIPELINE.work_has_failed_stage(stages, feishu_only=True))
        self.assertTrue(PIPELINE.work_has_failed_stage(stages, feishu_only=False))

    def test_feishu_only_status_still_fails_when_feishu_writeback_fails(self):
        stages = {
            "feishu_written_back": "failed",
            "backup_statuses_written_back": "success",
            "ima_backed_up": "skipped",
        }
        self.assertTrue(PIPELINE.work_has_failed_stage(stages, feishu_only=True))

    def test_feishu_only_status_fails_when_downstream_crashes_before_writeback(self):
        self.assertTrue(PIPELINE.work_has_failed_stage({"downstream": "failed"}, feishu_only=True))

    def test_ima_daily_quota_error_is_permanent_for_current_creator(self):
        self.assertTrue(PIPELINE.permanent_delivery_error("HTTP 403 / code 200005 / 请求超量，请明日再试"))

    def test_permanent_delivery_error_opens_creator_target_circuit_breaker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            creator = {"key": "a", "creator_name": "达人 A"}
            args = argparse.Namespace(
                dry_run=False, skip_feishu_writeback=True, skip_ima=False, skip_kuake=True,
                skip_obsidian=True, feishu_only=False, resume=False, force_stage=set(),
                overwrite=False, backup_workers=1, fail_fast=False, _delivery_breakers={},
            )
            runner = Mock()
            runner.run.side_effect = PIPELINE.PipelineError("IMA 凭证无效")
            for work_id in ("1", "2"):
                work = {"aweme_id": work_id}
                paths = PIPELINE.artifact_paths(root / "media", work_id, "volcengine")
                paths["final"].parent.mkdir(parents=True, exist_ok=True)
                paths["final"].write_text("文案", encoding="utf-8")
                result = PIPELINE.process_downstream(
                    {"ima": {"enabled": True}}, creator, work, root / "works.json", None,
                    root / "state", root / "media", runner,
                    PIPELINE.Logger(root / "log.txt", persist=False), {}, args,
                    {"aweme_id": work_id, "title": "ok", "stages": {"corrected": "success"}},
                )
                self.assertEqual(result["stages"]["ima_backed_up"], "failed")
            self.assertEqual(runner.run.call_count, 1)
            self.assertIn("ima_backed_up", args._delivery_breakers)

    def test_creator_batch_finalizer_calls_quark_and_feishu_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            creator = {"key": "a", "creator_name": "达人 A", "works_table_id": "tbl1"}
            selected = [{"aweme_id": "1", "create_time": 1}, {"aweme_id": "2", "create_time": 2}]
            delivery_results = {}
            for work in selected:
                work_id = work["aweme_id"]
                final_file = root / f"{work_id}.txt"
                final_file.write_text("文案", encoding="utf-8")
                delivery_results[work_id] = {
                    "aweme_id": work_id,
                    "stages": {
                        "ima_backed_up": "success", "kuake_backed_up": "pending",
                        "obsidian_exported": "success", "feishu_written_back": "pending",
                        "backup_statuses_written_back": "pending",
                    },
                    "_batch": {
                        "state_path": str(root / "state" / "a" / f"{work_id}.json"),
                        "transcript_file": str(final_file), "record_id": f"rec{work_id}",
                    },
                }
            runner = Mock()
            runner.run.side_effect = [
                json.dumps({"results": {"1": {"status": "success"}, "2": {"status": "success"}}}),
                json.dumps({"results": {"1": {"status": "success"}, "2": {"status": "failed", "error": "row locked"}}}),
            ]
            args = argparse.Namespace(dry_run=False, fail_fast=False)

            with patch.object(PIPELINE.time, "perf_counter", side_effect=[0.0, 4.0, 10.0, 16.0]):
                timings = PIPELINE.finalize_creator_batches(
                    {}, creator, selected, delivery_results, root / "state", runner,
                    PIPELINE.Logger(root / "log.txt", persist=False), {}, args,
                )

            self.assertEqual(runner.run.call_count, 2)
            self.assertIn("upload-manifest", runner.run.call_args_list[0].args[1])
            self.assertIn("--manifest", runner.run.call_args_list[1].args[1])
            self.assertEqual(delivery_results["1"]["stages"]["backup_statuses_written_back"], "success")
            self.assertEqual(delivery_results["2"]["stages"]["backup_statuses_written_back"], "failed")
            self.assertNotIn("_batch", delivery_results["1"])
            self.assertEqual(timings, {"kuake_batch_seconds": 4.0, "feishu_batch_seconds": 6.0})
            states = [
                json.loads((root / "state" / "a" / f"{work_id}.json").read_text(encoding="utf-8"))
                for work_id in ("1", "2")
            ]
            self.assertEqual(sum(item["stages"]["kuake_backed_up"]["duration_seconds"] for item in states), 4.0)
            self.assertEqual(sum(item["stages"]["feishu_written_back"]["duration_seconds"] for item in states), 6.0)
            self.assertEqual(states[0]["stages"]["kuake_backed_up"]["batch_duration_seconds"], 4.0)

    def test_quark_batch_recovers_completed_checkpoint_after_child_termination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            creator = {"key": "a", "creator_name": "达人 A"}
            selected = [{"aweme_id": "1", "create_time": 1}, {"aweme_id": "2", "create_time": 2}]
            delivery_results = {}
            for work in selected:
                work_id = work["aweme_id"]
                final_file = root / f"{work_id}.txt"
                final_file.write_text("文案", encoding="utf-8")
                delivery_results[work_id] = {
                    "aweme_id": work_id,
                    "stages": {"kuake_backed_up": "pending", "backup_statuses_written_back": "skipped"},
                    "_batch": {
                        "state_path": str(root / "state" / "a" / f"{work_id}.json"),
                        "transcript_file": str(final_file),
                    },
                }

            def terminate_after_checkpoint(label, command, env, **unused):
                manifest_path = Path(command[command.index("--manifest") + 1])
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                result_path = Path(manifest["result_file"])
                result_path.parent.mkdir(parents=True, exist_ok=True)
                result_path.write_text(json.dumps({
                    "batch_id": manifest["batch_id"],
                    "results": {"1": {"status": "success", "remote_path": "/done/1.txt"}},
                }, ensure_ascii=False), encoding="utf-8")
                raise PIPELINE.PipelineError("child terminated")

            runner = Mock()
            runner.run.side_effect = terminate_after_checkpoint
            PIPELINE.finalize_creator_batches(
                {}, creator, selected, delivery_results, root / "state", runner,
                PIPELINE.Logger(root / "log.txt", persist=False), {},
                argparse.Namespace(dry_run=False, fail_fast=False),
            )

            self.assertEqual(delivery_results["1"]["stages"]["kuake_backed_up"], "success")
            self.assertEqual(delivery_results["2"]["stages"]["kuake_backed_up"], "failed")

    def test_creator_mapping_confirmation_overlaps_asr_preparation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            creator = {"key": "a", "creator_name": "达人 A"}
            work = {"aweme_id": "1"}
            context = {
                "creator": creator, "key": "a", "name": "达人 A",
                "works_file": root / "works.json", "profile_file": None,
                "state_dir": root / "state", "media_dir": root / "media",
                "collection_state_file": root / "collection.json",
                "result": {"key": "a", "works": [], "phase_timings": {}},
                "selected": [work], "collection_ok": True, "sync_ok": True,
                "synced_record_ids": {}, "terminal": False,
            }
            args = argparse.Namespace(asr_workers=1, fail_fast=False, dry_run=True)
            mapping_started = threading.Event()
            preparation_started = threading.Event()

            def mappings(*unused):
                mapping_started.set()
                self.assertTrue(preparation_started.wait(2), "目录映射确认阻塞了 ASR")
                return {"ima": "success", "kuake": "success", "obsidian": "success"}

            def prepare(*unused):
                preparation_started.set()
                self.assertTrue(mapping_started.wait(2), "ASR 未与目录映射确认同时启动")
                return {"aweme_id": "1", "title": "ok", "stages": {"transcribed": "success", "corrected": "success"}}

            with patch.object(PIPELINE, "sync_creator_backup_mappings", side_effect=mappings), patch.object(
                PIPELINE, "prepare_work", side_effect=prepare,
            ), patch.object(PIPELINE, "process_downstream", side_effect=lambda *values: values[-2]):
                result = PIPELINE.process_creator_phase(
                    {}, context, Mock(), PIPELINE.Logger(root / "log.txt", persist=False), {}, args,
                )

            self.assertEqual(result["works"][0]["aweme_id"], "1")

    def test_creator_delivers_in_asr_completion_order_but_reports_selected_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            creator = {"key": "a", "creator_name": "达人 A"}
            works = [{"aweme_id": "1"}, {"aweme_id": "2"}]
            context = {
                "creator": creator, "key": "a", "name": "达人 A",
                "works_file": root / "works.json", "profile_file": None,
                "state_dir": root / "state", "media_dir": root / "media",
                "collection_state_file": root / "collection.json",
                "result": {"key": "a", "works": [], "phase_timings": {}},
                "selected": works, "collection_ok": True, "sync_ok": True,
                "synced_record_ids": {}, "terminal": False,
            }
            args = argparse.Namespace(asr_workers=2, fail_fast=False, dry_run=True)
            release_first = threading.Event()
            second_delivered = threading.Event()
            delivered: list[str] = []

            def prepare(config, creator, work, *unused):
                if work["aweme_id"] == "1":
                    self.assertTrue(release_first.wait(2), "第一条作品过早完成")
                return {"aweme_id": work["aweme_id"], "title": "ok", "stages": {"transcribed": "success", "corrected": "success"}}

            def deliver(config, creator, work, *values):
                delivered.append(work["aweme_id"])
                if work["aweme_id"] == "2":
                    second_delivered.set()
                    release_first.set()
                return values[-2]

            with patch.object(PIPELINE, "sync_creator_backup_mappings", return_value={}), patch.object(
                PIPELINE, "prepare_work", side_effect=prepare,
            ), patch.object(PIPELINE, "process_downstream", side_effect=deliver):
                result = PIPELINE.process_creator_phase(
                    {}, context, Mock(), PIPELINE.Logger(root / "log.txt", persist=False), {}, args,
                )

            self.assertTrue(second_delivered.is_set())
            self.assertEqual(delivered, ["2", "1"])
            self.assertEqual([item["aweme_id"] for item in result["works"]], ["1", "2"])

    def test_main_overlaps_next_creator_collection_with_previous_creator_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "pipeline.json"
            config_path.write_text(
                json.dumps({
                    "state_dir": str(root / "state"),
                    "log_dir": str(root / "logs"),
                    "creators": [
                        {"key": "a", "creator_name": "达人 A"},
                        {"key": "b", "creator_name": "达人 B"},
                    ],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            a_processing_started = threading.Event()
            b_collection_started = threading.Event()
            a_processing_finished = threading.Event()
            events: list[str] = []

            def collect_phase(config, creator, runner, logger, env, args):
                key = creator["key"]
                events.append(f"collect:{key}")
                if key == "b":
                    self.assertTrue(a_processing_started.wait(5), "达人 A 文案处理没有与达人 B 采集重叠")
                    self.assertFalse(a_processing_finished.is_set())
                    b_collection_started.set()
                return {"creator": creator, "result": {"key": key, "works": []}}

            def process_phase(config, context, runner, logger, env, args):
                key = context["creator"]["key"]
                events.append(f"process:{key}:start")
                if key == "a":
                    a_processing_started.set()
                    self.assertTrue(b_collection_started.wait(5), "达人 B 采集等待了达人 A 文案处理完成")
                    a_processing_finished.set()
                else:
                    self.assertTrue(a_processing_finished.is_set(), "达人 B 文案处理早于达人 A 完成")
                events.append(f"process:{key}:end")
                return {"key": key, "status": "success", "works": []}

            with patch.object(PIPELINE, "collect_creator_phase", side_effect=collect_phase), patch.object(
                PIPELINE, "process_creator_phase", side_effect=process_phase,
            ):
                exit_code = PIPELINE.main(["--config", str(config_path), "--dry-run"])

            self.assertEqual(exit_code, 0)
            self.assertLess(events.index("collect:b"), events.index("process:a:end"))
            self.assertLess(events.index("process:a:end"), events.index("process:b:start"))

    def test_main_collects_three_creators_concurrently_and_serially_retries_only_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "pipeline.json"
            config_path.write_text(
                json.dumps({
                    "state_dir": str(root / "state"),
                    "log_dir": str(root / "logs"),
                    "creators": [{"key": "a"}, {"key": "b"}, {"key": "c"}],
                }),
                encoding="utf-8",
            )
            attempts = {"a": 0, "b": 0, "c": 0}
            collection_lock = threading.Lock()
            all_initial_started = threading.Event()
            active_collections = 0
            max_active_collections = 0
            retry_active_snapshots = []
            active_processing = 0
            max_active_processing = 0
            processing_lock = threading.Lock()
            output = []

            def collect_phase(config, creator, runner, logger, env, args):
                nonlocal active_collections, max_active_collections
                key = creator["key"]
                with collection_lock:
                    attempts[key] += 1
                    attempt = attempts[key]
                    if attempt == 1:
                        active_collections += 1
                        max_active_collections = max(max_active_collections, active_collections)
                        if active_collections == 3:
                            all_initial_started.set()
                    else:
                        retry_active_snapshots.append(active_collections)
                if attempt == 1:
                    all_initial_started.wait(1)
                    with collection_lock:
                        active_collections -= 1
                    if key == "b":
                        raise RuntimeError("parallel browser conflict")
                return {"creator": creator, "result": {"key": key, "works": []}}

            def process_phase(config, context, runner, logger, env, args):
                nonlocal active_processing, max_active_processing
                with processing_lock:
                    active_processing += 1
                    max_active_processing = max(max_active_processing, active_processing)
                threading.Event().wait(0.02)
                with processing_lock:
                    active_processing -= 1
                return {"key": context["creator"]["key"], "status": "success", "works": []}

            with patch.object(PIPELINE, "collect_creator_phase", side_effect=collect_phase), patch.object(
                PIPELINE, "process_creator_phase", side_effect=process_phase,
            ), patch.object(PIPELINE, "print_console", side_effect=lambda value, **kwargs: output.append(str(value))):
                exit_code = PIPELINE.main(["--config", str(config_path), "--dry-run"])

            summary = json.loads(output[-1])
            self.assertEqual(exit_code, 0)
            self.assertEqual(max_active_collections, 3)
            self.assertEqual(attempts, {"a": 1, "b": 2, "c": 1})
            self.assertEqual(retry_active_snapshots, [0])
            self.assertEqual(max_active_processing, 1)
            self.assertEqual([item["key"] for item in summary["creators"]], ["a", "b", "c"])
            self.assertTrue(all(item["status"] == "success" for item in summary["creators"]))

    def test_processing_failure_is_recorded_and_releases_next_creator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "pipeline.json"
            config_path.write_text(
                json.dumps({
                    "state_dir": str(root / "state"),
                    "log_dir": str(root / "logs"),
                    "creators": [{"key": "a"}, {"key": "b"}],
                }),
                encoding="utf-8",
            )
            processed: list[str] = []
            output: list[str] = []

            def collect_phase(config, creator, runner, logger, env, args):
                return {"creator": creator, "result": {"key": creator["key"], "works": []}}

            def process_phase(config, context, runner, logger, env, args):
                key = context["creator"]["key"]
                processed.append(key)
                if key == "a":
                    raise RuntimeError("ASR unavailable")
                return {"key": key, "status": "success", "works": []}

            with patch.object(PIPELINE, "collect_creator_phase", side_effect=collect_phase), patch.object(
                PIPELINE, "process_creator_phase", side_effect=process_phase,
            ), patch.object(PIPELINE, "print_console", side_effect=lambda value, **kwargs: output.append(str(value))):
                exit_code = PIPELINE.main(["--config", str(config_path), "--dry-run"])

            summary = json.loads(output[-1])
            self.assertEqual(exit_code, 1)
            self.assertEqual(processed, ["a", "b"])
            self.assertEqual(summary["creators"][0]["status"], "failed")
            self.assertIn("ASR unavailable", summary["creators"][0]["error"])
            self.assertIn("transcript_processing_seconds", summary["creators"][0]["phase_timings"])
            self.assertEqual(summary["creators"][1]["status"], "success")

    def test_collection_failure_records_timing_and_does_not_stop_next_creator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "pipeline.json"
            config_path.write_text(
                json.dumps({
                    "state_dir": str(root / "state"),
                    "log_dir": str(root / "logs"),
                    "creators": [{"key": "a"}, {"key": "b"}],
                }),
                encoding="utf-8",
            )
            collected: list[str] = []
            processed: list[str] = []
            output: list[str] = []

            def collect_phase(config, creator, runner, logger, env, args):
                key = creator["key"]
                collected.append(key)
                if key == "a":
                    raise RuntimeError("collector unavailable")
                return {"creator": creator, "result": {"key": key, "works": []}}

            def process_phase(config, context, runner, logger, env, args):
                key = context["creator"]["key"]
                processed.append(key)
                return {"key": key, "status": "success", "works": []}

            with patch.object(PIPELINE, "collect_creator_phase", side_effect=collect_phase), patch.object(
                PIPELINE, "process_creator_phase", side_effect=process_phase,
            ), patch.object(PIPELINE, "print_console", side_effect=lambda value, **kwargs: output.append(str(value))):
                exit_code = PIPELINE.main(["--config", str(config_path), "--dry-run"])

            summary = json.loads(output[-1])
            self.assertEqual(exit_code, 1)
            self.assertCountEqual(collected, ["a", "a", "b"])
            self.assertEqual(processed, ["b"])
            self.assertEqual(summary["creators"][0]["status"], "failed")
            self.assertEqual(summary["creators"][0]["collection_attempts"], 2)
            self.assertTrue(summary["creators"][0]["fallback_to_serial"])
            self.assertIn("collector unavailable", summary["creators"][0]["error"])
            self.assertIn("collection_and_sync_seconds", summary["creators"][0]["phase_timings"])
            self.assertIn("serial_retry_seconds", summary["creators"][0]["phase_timings"])
            self.assertEqual(summary["creators"][1]["status"], "success")

    def test_collection_partial_failure_result_is_serially_retried_before_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "pipeline.json"
            config_path.write_text(
                json.dumps({
                    "state_dir": str(root / "state"),
                    "log_dir": str(root / "logs"),
                    "creators": [{"key": "a"}, {"key": "b"}],
                }),
                encoding="utf-8",
            )
            attempts = {"a": 0, "b": 0}
            processed: list[str] = []
            output: list[str] = []

            def collect_phase(config, creator, runner, logger, env, args):
                key = creator["key"]
                attempts[key] += 1
                if key == "a" and attempts[key] == 1:
                    return {
                        "creator": creator,
                        "result": {
                            "key": key,
                            "status": "partial_failure",
                            "collection_error": "collector unavailable",
                            "works": [],
                        },
                        "terminal": True,
                    }
                return {"creator": creator, "result": {"key": key, "works": []}}

            def process_phase(config, context, runner, logger, env, args):
                result = context["result"]
                key = context["creator"]["key"]
                processed.append(key)
                result["status"] = "success"
                return result

            with patch.object(PIPELINE, "collect_creator_phase", side_effect=collect_phase), patch.object(
                PIPELINE, "process_creator_phase", side_effect=process_phase,
            ), patch.object(PIPELINE, "print_console", side_effect=lambda value, **kwargs: output.append(str(value))):
                exit_code = PIPELINE.main(["--config", str(config_path), "--dry-run"])

            summary = json.loads(output[-1])
            creator_a = summary["creators"][0]
            self.assertEqual(exit_code, 0)
            self.assertEqual(attempts, {"a": 2, "b": 1})
            self.assertCountEqual(processed, ["a", "b"])
            self.assertEqual(creator_a["collection_attempts"], 2)
            self.assertTrue(creator_a["fallback_to_serial"])
            self.assertEqual(creator_a["parallel_collection_error"], "collector unavailable")
            self.assertIn("parallel_collection_seconds", creator_a["phase_timings"])
            self.assertIn("serial_retry_seconds", creator_a["phase_timings"])

    def test_collection_partial_failure_retry_is_recorded_without_processing_creator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "pipeline.json"
            config_path.write_text(
                json.dumps({
                    "state_dir": str(root / "state"),
                    "log_dir": str(root / "logs"),
                    "creators": [{"key": "a"}, {"key": "b"}],
                }),
                encoding="utf-8",
            )
            attempts = {"a": 0, "b": 0}
            processed: list[str] = []
            output: list[str] = []

            def collect_phase(config, creator, runner, logger, env, args):
                key = creator["key"]
                attempts[key] += 1
                if key == "a":
                    error = "parallel failure" if attempts[key] == 1 else "serial retry failure"
                    return {
                        "creator": creator,
                        "result": {
                            "key": key,
                            "status": "partial_failure",
                            "collection_error": error,
                            "works": [],
                        },
                        "terminal": True,
                    }
                return {"creator": creator, "result": {"key": key, "works": []}}

            def process_phase(config, context, runner, logger, env, args):
                result = context["result"]
                key = context["creator"]["key"]
                processed.append(key)
                result["status"] = "success"
                return result

            with patch.object(PIPELINE, "collect_creator_phase", side_effect=collect_phase), patch.object(
                PIPELINE, "process_creator_phase", side_effect=process_phase,
            ), patch.object(PIPELINE, "print_console", side_effect=lambda value, **kwargs: output.append(str(value))):
                exit_code = PIPELINE.main(["--config", str(config_path), "--dry-run"])

            summary = json.loads(output[-1])
            creator_a = summary["creators"][0]
            self.assertEqual(exit_code, 1)
            self.assertEqual(attempts, {"a": 2, "b": 1})
            self.assertEqual(processed, ["b"])
            self.assertEqual(creator_a["status"], "partial_failure")
            self.assertEqual(creator_a["collection_error"], "serial retry failure")
            self.assertEqual(creator_a["parallel_collection_error"], "parallel failure")
            self.assertEqual(creator_a["collection_attempts"], 2)
            self.assertTrue(creator_a["fallback_to_serial"])
            self.assertIn("parallel_collection_seconds", creator_a["phase_timings"])
            self.assertIn("serial_retry_seconds", creator_a["phase_timings"])

    def test_creator_processing_records_unexpected_work_failure_and_continues_remaining_works(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            creator = {"key": "a", "creator_name": "达人 A"}
            works = [
                {"aweme_id": "1", "create_time": 2},
                {"aweme_id": "2", "create_time": 1},
            ]
            context = {
                "creator": creator,
                "key": "a",
                "name": "达人 A",
                "works_file": root / "works.json",
                "profile_file": None,
                "state_dir": root / "state",
                "media_dir": root / "media",
                "collection_state_file": root / "collection.json",
                "result": {"key": "a", "works": [], "phase_timings": {}},
                "selected": works,
                "collection_ok": True,
                "sync_ok": True,
                "synced_record_ids": {},
                "terminal": False,
            }
            args = argparse.Namespace(asr_workers=1, fail_fast=False, dry_run=False)
            prepared_ids: list[str] = []
            delivered_ids: list[str] = []

            def prepare(config, creator, work, works_file, profile_file, state_dir, media_dir, runner, logger, env, args):
                work_id = work["aweme_id"]
                prepared_ids.append(work_id)
                if work_id == "1":
                    raise RuntimeError("bad audio")
                return {"aweme_id": work_id, "title": "ok", "stages": {"transcribed": "success", "corrected": "success"}}

            def deliver(config, creator, work, works_file, profile_file, state_dir, media_dir, runner, logger, env, args, result, record_id):
                delivered_ids.append(work["aweme_id"])
                return result

            with patch.object(PIPELINE, "sync_creator_backup_mappings", return_value={}), patch.object(
                PIPELINE, "prepare_work", side_effect=prepare,
            ), patch.object(PIPELINE, "process_downstream", side_effect=deliver):
                result = PIPELINE.process_creator_phase(
                    {}, context, Mock(), PIPELINE.Logger(root / "log.txt", persist=False), {}, args,
                )

            self.assertEqual(prepared_ids, ["1", "2"])
            self.assertEqual(delivered_ids, ["1", "2"])
            self.assertEqual(result["works"][0]["stages"]["transcribed"], "failed")
            self.assertIn("bad audio", result["works"][0]["error"])
            self.assertEqual(result["works"][1]["stages"]["transcribed"], "success")
            self.assertEqual(result["status"], "partial_failure")
            persisted = json.loads((root / "state" / "a" / "1.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["stages"]["transcribed"]["status"], "failed")
            self.assertEqual(persisted["stages"]["corrected"]["status"], "blocked")

    def test_creator_processing_records_unexpected_delivery_failure_and_continues_remaining_works(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            creator = {"key": "a", "creator_name": "达人 A"}
            works = [{"aweme_id": "1"}, {"aweme_id": "2"}]
            context = {
                "creator": creator,
                "key": "a",
                "name": "达人 A",
                "works_file": root / "works.json",
                "profile_file": None,
                "state_dir": root / "state",
                "media_dir": root / "media",
                "collection_state_file": root / "collection.json",
                "result": {"key": "a", "works": [], "phase_timings": {}},
                "selected": works,
                "collection_ok": True,
                "sync_ok": True,
                "synced_record_ids": {},
                "terminal": False,
            }
            args = argparse.Namespace(asr_workers=1, fail_fast=False, dry_run=False)
            delivered_ids: list[str] = []

            def prepare(config, creator, work, works_file, profile_file, state_dir, media_dir, runner, logger, env, args):
                return {
                    "aweme_id": work["aweme_id"], "title": "ok",
                    "stages": {"transcribed": "success", "corrected": "success"},
                }

            def deliver(config, creator, work, works_file, profile_file, state_dir, media_dir, runner, logger, env, args, result, record_id):
                work_id = work["aweme_id"]
                delivered_ids.append(work_id)
                if work_id == "1":
                    raise RuntimeError("writeback unavailable")
                return result

            with patch.object(PIPELINE, "sync_creator_backup_mappings", return_value={}), patch.object(
                PIPELINE, "prepare_work", side_effect=prepare,
            ), patch.object(PIPELINE, "process_downstream", side_effect=deliver):
                result = PIPELINE.process_creator_phase(
                    {}, context, Mock(), PIPELINE.Logger(root / "log.txt", persist=False), {}, args,
                )

            self.assertEqual(delivered_ids, ["1", "2"])
            self.assertEqual(result["works"][0]["stages"]["downstream"], "failed")
            self.assertIn("writeback unavailable", result["works"][0]["error"])
            self.assertEqual(result["works"][1]["stages"]["transcribed"], "success")
            self.assertEqual(result["status"], "partial_failure")
            persisted = json.loads((root / "state" / "a" / "1.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["stages"]["downstream"]["status"], "failed")

    def test_select_works_filters_sorts_and_limits(self):
        works = [
            {"aweme_id": "old", "create_time": 1},
            {"aweme_id": "new", "create_time": 3},
            {"aweme_id": "middle", "create_time": 2},
        ]
        selected = PIPELINE.select_works(works, set(), 2)
        self.assertEqual([item["aweme_id"] for item in selected], ["new", "middle"])
        selected = PIPELINE.select_works(works, {"middle"}, 0)
        self.assertEqual([item["aweme_id"] for item in selected], ["middle"])

    def test_write_selected_works_file_limits_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            source = directory_path / "works.json"
            output = directory_path / "selected" / "works.json"
            source.write_text(
                json.dumps({"count": 3, "works": [{"aweme_id": "1"}, {"aweme_id": "2"}, {"aweme_id": "3"}]}),
                encoding="utf-8",
            )

            PIPELINE.write_selected_works_file(source, [{"aweme_id": "2"}], output)

            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["works"], [{"aweme_id": "2"}])
            self.assertEqual(payload["source_file"], str(source))
            self.assertIn("selected_at", payload)

    def test_audio_url_prefers_normalized_then_raw(self):
        self.assertEqual(PIPELINE.audio_url({"music_download_url": "direct", "raw": {"music_download_url": "raw"}}), "direct")
        self.assertEqual(PIPELINE.audio_url({"raw": {"music_download_url": ["raw-list"]}}), "raw-list")
        self.assertEqual(PIPELINE.audio_url({"raw": {"video_download_url": "video"}}), "video")

    def test_fallback_work_text_prefers_raw_metadata(self):
        self.assertEqual(
            PIPELINE.fallback_work_text({"desc": "发布文案", "raw": {"title": "标题"}}),
            "标题",
        )

    def test_asr_source_failure_can_fall_back_to_published_text(self):
        self.assertTrue(PIPELINE.should_fallback_to_work_text(
            "ASR query code 20000003: [Normal silence audio] no valid speech",
        ))
        self.assertTrue(PIPELINE.should_fallback_to_work_text(
            "ASR query code 45000006: Invalid audio URI; audio download failed",
        ))
        self.assertFalse(PIPELINE.should_fallback_to_work_text("temporary HTTP 503"))

    def test_sensitive_command_values_are_masked(self):
        command = ["python", "script.py", "--audio-url", "signed-url", "--table-id", "tbl-secret"]
        shown = PIPELINE.Runner.display(command, ("--audio-url", "--table-id"))
        self.assertNotIn("signed-url", shown)
        self.assertNotIn("tbl-secret", shown)
        self.assertEqual(shown.count("<redacted>"), 2)

    def test_artifact_paths_keep_existing_naming_convention(self):
        paths = PIPELINE.artifact_paths(Path("runtime/media"), "123", "volcengine")
        self.assertEqual(paths["raw_text"].name, "123.volc-url.txt")
        self.assertEqual(paths["final"].name, "123.final.txt")

    def test_asr_worker_count_uses_cli_then_config_and_caps_to_work_count(self):
        args = argparse.Namespace(asr_workers=None)
        self.assertEqual(PIPELINE.asr_worker_count({"asr": {"max_workers": 6}}, args, 3), 3)
        args.asr_workers = 2
        self.assertEqual(PIPELINE.asr_worker_count({"asr": {"max_workers": 6}}, args, 9), 2)

    def test_asr_worker_count_rejects_zero(self):
        args = argparse.Namespace(asr_workers=0)
        with self.assertRaises(PIPELINE.PipelineError):
            PIPELINE.asr_worker_count({}, args, 10)

    def test_final_backup_status_maps_pipeline_outcomes(self):
        self.assertEqual(PIPELINE.final_backup_status("success", "已上传"), "已上传")
        self.assertEqual(PIPELINE.final_backup_status("skipped", "已写入"), "已写入")
        self.assertEqual(PIPELINE.final_backup_status("disabled", "已上传"), "跳过")
        self.assertEqual(PIPELINE.final_backup_status("blocked", "已上传"), "失败")
        self.assertEqual(PIPELINE.final_backup_status("failed", "已写入"), "失败")

    def test_status_writeback_command_contains_all_final_statuses(self):
        config = {"python": "python", "feishu": {"work_id_field": "抖音作品ID", "as_identity": "user"}}
        creator = {"key": "demo", "works_table_id": "tbl-demo"}
        work = {"aweme_id": "123"}
        stages = {"ima_backed_up": "success", "kuake_backed_up": "disabled", "obsidian_exported": "failed"}
        command = PIPELINE.status_writeback_command(config, creator, work, stages)
        self.assertIn("feishu_work_status_writer.py", command[1])
        self.assertEqual(command[command.index("--ima-status") + 1], "已上传")
        self.assertEqual(command[command.index("--kuake-status") + 1], "跳过")
        self.assertEqual(command[command.index("--local-status") + 1], "失败")

    def test_sync_record_ids_are_reused_by_writeback_commands(self):
        output = json.dumps({"record_ids": {"123": "rec123"}})
        self.assertEqual(PIPELINE.parse_sync_record_ids(output), {"123": "rec123"})
        config = {"python": "python", "feishu": {}}
        creator = {"key": "demo", "works_table_id": "tbl-demo"}
        work = {"aweme_id": "123"}
        paths = {"final": Path("final.txt")}
        transcript = PIPELINE.writeback_command(config, creator, work, paths, "rec123")
        self.assertEqual(transcript[transcript.index("--record-id") + 1], "rec123")
        statuses = PIPELINE.status_writeback_command(config, creator, work, {}, "rec123")
        self.assertEqual(statuses[statuses.index("--record-id") + 1], "rec123")
        combined = PIPELINE.status_writeback_command(config, creator, work, {}, "rec123", Path("final.txt"))
        self.assertEqual(combined[combined.index("--transcript-file") + 1], "final.txt")

    def test_mapping_cache_requires_valid_identity_and_success_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / "ima.json", root / "kuake.json", root / "obsidian.json"]
            creator_name = "示例达人"
            for path_value in paths:
                payload = {
                    "platform": path_value.stem,
                    "directory": {"name": creator_name, "path": f"/root/{creator_name}"},
                    "feishu_fields": {"同步状态": "已映射"},
                }
                path_value.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            marker = root / "feishu-sync.json"
            marker.write_text(json.dumps({"signature": PIPELINE.mapping_cache_signature(paths)}), encoding="utf-8")
            now_epoch = paths[0].stat().st_mtime + 60
            self.assertTrue(PIPELINE.mapping_cache_is_fresh(
                paths, ttl_hours=24, now_epoch=now_epoch,
                marker_path=marker, expected_creator_name=creator_name,
            ))
            payload = json.loads(paths[-1].read_text(encoding="utf-8"))
            payload["directory"]["name"] = "另一个达人"
            paths[-1].write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            self.assertFalse(PIPELINE.mapping_cache_is_fresh(
                paths, ttl_hours=24, now_epoch=now_epoch,
                marker_path=marker, expected_creator_name=creator_name,
            ))

    def test_logger_replaces_characters_unsupported_by_console_encoding(self):
        class GbkStream(io.StringIO):
            encoding = "gbk"

        stream = GbkStream()
        logger = PIPELINE.Logger(Path("unused.log"), persist=False)
        with patch.object(PIPELINE.sys, "stdout", stream):
            logger.write("unsupported: ʹ")
        self.assertIn("unsupported: ?", stream.getvalue())

    def test_runner_records_precise_success_timing(self):
        logger = PIPELINE.Logger(Path("unused.log"), persist=False)
        runner = PIPELINE.Runner(logger, dry_run=False)
        completed = Mock(returncode=0, stdout="ok", stderr="")
        with patch.object(PIPELINE.subprocess, "run", return_value=completed), patch.object(
            PIPELINE.time, "perf_counter", side_effect=[10.0, 12.25]
        ):
            runner.run("测试阶段", ["tool"], {})
        self.assertEqual(runner.metrics[0]["label"], "测试阶段")
        self.assertEqual(runner.metrics[0]["status"], "success")
        self.assertEqual(runner.metrics[0]["seconds"], 2.25)

    def test_runner_records_timing_when_subprocess_raises(self):
        logger = PIPELINE.Logger(Path("unused.log"), persist=False)
        runner = PIPELINE.Runner(logger, dry_run=False)
        with patch.object(PIPELINE.subprocess, "run", side_effect=OSError("cannot start")), patch.object(
            PIPELINE.time, "perf_counter", side_effect=[10.0, 10.5]
        ):
            with self.assertRaises(OSError):
                runner.run("启动失败", ["tool"], {})
        self.assertEqual(runner.metrics[0], {"label": "启动失败", "seconds": 0.5, "status": "failed"})

    def test_combined_finalizer_duration_is_not_double_counted(self):
        state = {}
        PIPELINE.set_combined_finalizer_status(
            state, transcript_included=True, status="success", duration_seconds=2.5,
        )
        self.assertEqual(state["stages"]["feishu_written_back"]["duration_seconds"], 2.5)
        self.assertEqual(state["stages"]["backup_statuses_written_back"]["duration_seconds"], 0.0)
        self.assertEqual(
            state["stages"]["feishu_written_back"]["operation_id"],
            state["stages"]["backup_statuses_written_back"]["operation_id"],
        )

    def test_stage_state_and_summary_keep_duration_metrics(self):
        state = {}
        PIPELINE.set_status(state, "ima_backed_up", "success", duration_seconds=3.125)
        self.assertEqual(state["stages"]["ima_backed_up"]["duration_seconds"], 3.125)
        summary = PIPELINE.summarize_metrics(
            [
                {"label": "飞书同步", "seconds": 2.0, "status": "success"},
                {"label": "夸克备份", "seconds": 5.0, "status": "failed"},
            ]
        )
        self.assertEqual(summary["total_calls"], 2)
        self.assertEqual(summary["total_seconds"], 7.0)
        self.assertEqual(summary["failed_calls"], 1)

    def test_early_configuration_failure_still_emits_run_timing_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "invalid.json"
            config_path.write_text('{"creators": []}', encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), patch.object(
                PIPELINE.time, "perf_counter", side_effect=[10.0, 10.25]
            ):
                exit_code = PIPELINE.main(["--config", str(config_path), "--dry-run"])
        self.assertEqual(exit_code, 2)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["wall_seconds"], 0.25)
        self.assertIn("error", payload)

    def test_independent_backup_actions_isolate_failures(self):
        actions = [
            ("ima_backed_up", lambda: None),
            ("kuake_backed_up", lambda: (_ for _ in ()).throw(RuntimeError("network"))),
            ("obsidian_exported", lambda: None),
        ]
        outcomes = PIPELINE.run_independent_actions(actions, max_workers=3, dry_run=False)
        self.assertEqual(outcomes["ima_backed_up"]["status"], "success")
        self.assertEqual(outcomes["kuake_backed_up"]["status"], "failed")
        self.assertIn("network", outcomes["kuake_backed_up"]["error"])
        self.assertEqual(outcomes["obsidian_exported"]["status"], "success")

    def test_backup_worker_count_is_bounded_by_runnable_stages(self):
        args = argparse.Namespace(backup_workers=None)
        self.assertEqual(PIPELINE.backup_worker_count({"backups": {"max_workers": 3}}, args, 2), 2)
        args.backup_workers = 1
        self.assertEqual(PIPELINE.backup_worker_count({"backups": {"max_workers": 3}}, args, 3), 1)

    def test_mapping_sync_command_combines_provider_metadata(self):
        config = {"python": "python", "feishu": {"creator_table_id": "tbl-creators"}}
        creator = {"key": "demo", "creator_name": "示例达人"}
        command = PIPELINE.mapping_sync_command(
            config, creator, [Path("ima.json"), Path("kuake.json"), Path("obsidian.json")]
        )
        self.assertEqual(command.count("--metadata-file"), 3)

    def test_resume_skips_finalizer_when_nothing_changed(self):
        stages = {
            "ima_backed_up": "skipped",
            "kuake_backed_up": "skipped",
            "obsidian_exported": "skipped",
        }
        self.assertFalse(PIPELINE.finalizer_needed(stages, None, "success", resume=True))
        self.assertTrue(PIPELINE.finalizer_needed({**stages, "kuake_backed_up": "failed"}, None, "success", resume=True))
        self.assertTrue(PIPELINE.finalizer_needed(stages, Path("final.txt"), "success", resume=True))


    def test_pending_collection_ids_prefers_state_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_file = root / "collection.json"
            works_file = root / "works.json"
            state_file.write_text(json.dumps({"pending_aweme_ids": ["3", "2", "2"]}), encoding="utf-8")
            works_file.write_text(json.dumps({"pending_aweme_ids": ["1"]}), encoding="utf-8")
            self.assertEqual(PIPELINE.pending_collection_ids(state_file, works_file), ["3", "2"])

    def test_complete_collection_pending_only_removes_processed_subset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_file = root / "collection.json"
            works_file = root / "works.json"
            payload = {"pending_aweme_ids": ["new-3", "new-2", "new-1"], "pending_count": 3}
            state_file.write_text(json.dumps(payload), encoding="utf-8")
            works_file.write_text(json.dumps(payload), encoding="utf-8")

            PIPELINE.complete_collection_pending(state_file, works_file, ["new-3"])

            for path in (state_file, works_file):
                updated = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(updated["pending_aweme_ids"], ["new-2", "new-1"])
                self.assertEqual(updated["pending_count"], 2)
                self.assertIn("last_pipeline_completed_at", updated)

    def test_max_works_limits_pending_selection_without_discarding_remainder(self):
        works = [
            {"aweme_id": "new-3", "create_time": 3},
            {"aweme_id": "new-2", "create_time": 2},
            {"aweme_id": "new-1", "create_time": 1},
        ]
        selected = PIPELINE.select_works(works, {"new-3", "new-2", "new-1"}, 1)
        self.assertEqual([item["aweme_id"] for item in selected], ["new-3"])

    def test_collect_command_passes_incremental_state_and_probe(self):
        config = {
            "python": "python",
            "collection": {
                "incremental_probe_count": 3,
                "incremental_enabled": True,
                "cdp_port_start": 9222,
                "cdp_port_stride": 10,
            },
            "creators": [{"key": "a"}, {"key": "b"}, {"key": "demo"}],
        }
        creator = {"key": "demo", "creator_url": "creator-id"}
        command = PIPELINE.collect_command(
            config,
            creator,
            Path("works.json"),
            Path("media-output"),
            Path("collection-state.json"),
            False,
        )
        self.assertIn("--collection-state-file", command)
        self.assertIn("collection-state.json", command)
        self.assertIn("--incremental-probe-count", command)
        self.assertNotIn("--force-full-collect", command)
        self.assertEqual(command[command.index("--browser-profile-key") + 1], "demo")
        self.assertEqual(command[command.index("--cdp-port") + 1], "9242")

        forced = PIPELINE.collect_command(
            config,
            creator,
            Path("works.json"),
            Path("media-output"),
            Path("collection-state.json"),
            False,
            True,
        )
        self.assertIn("--force-full-collect", forced)


    def test_backfill_selection_advances_after_local_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            media_dir = root / "media"
            creator = {"key": "demo", "creator_name": "示例"}
            config = {
                "asr": {"provider": "volcengine"},
                "ima": {"enabled": True},
                "kuake": {"enabled": True},
                "obsidian": {"enabled": True},
            }
            args = argparse.Namespace(skip_ima=False, skip_kuake=False, skip_obsidian=False)
            works = [
                {"aweme_id": "new", "create_time": 2},
                {"aweme_id": "old", "create_time": 1},
            ]
            for work in works:
                paths = PIPELINE.artifact_paths(media_dir, work["aweme_id"], "volcengine")
                paths["final"].parent.mkdir(parents=True, exist_ok=True)
                paths["final"].write_text("文案", encoding="utf-8")
                state = PIPELINE.load_state(
                    state_dir / "demo" / f"{work['aweme_id']}.json", creator, work,
                )
                for stage in ("ima_backed_up", "kuake_backed_up", "obsidian_exported"):
                    PIPELINE.set_status(state, stage, "success")
                PIPELINE.write_json(state_dir / "demo" / f"{work['aweme_id']}.json", state)
            (media_dir / "new.final.txt").unlink()

            selected = PIPELINE.select_backfill_works(
                config, creator, works, state_dir, media_dir, args, 1,
            )
            self.assertEqual([item["aweme_id"] for item in selected], ["new"])

    def test_prepare_work_seeds_text_only_aweme_without_external_asr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = {"aweme_id": "note-1", "create_time": 1, "desc": "图文作品发布文案"}
            creator = {"key": "demo", "creator_name": "示例"}
            args = argparse.Namespace(
                dry_run=False, skip_transcribe=False, skip_correction=True, resume=True,
                force_stage=set(),
            )
            runner = Mock()
            result = PIPELINE.prepare_work(
                {"asr": {"provider": "volcengine"}}, creator, work, root / "works.json",
                None, root / "state", root / "media", runner,
                PIPELINE.Logger(root / "log.txt", persist=False), {}, args,
            )
            runner.run.assert_not_called()
            paths = PIPELINE.artifact_paths(root / "media", "note-1", "volcengine")
            self.assertEqual(paths["raw_text"].read_text(encoding="utf-8"), "图文作品发布文案")
            self.assertEqual(result["stages"]["transcribed"], "skipped")

    def test_skip_feishu_writeback_never_runs_finalizer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_dir = root / "media"
            state_dir = root / "state"
            work = {"aweme_id": "123", "create_time": 1}
            creator = {"key": "demo", "creator_name": "示例"}
            paths = PIPELINE.artifact_paths(media_dir, "123", "volcengine")
            paths["final"].parent.mkdir(parents=True, exist_ok=True)
            paths["final"].write_text("最终文案", encoding="utf-8")
            args = argparse.Namespace(
                dry_run=False, skip_feishu_writeback=True, skip_ima=True, skip_kuake=True,
                skip_obsidian=True, resume=True, force_stage=set(), fail_fast=False,
                overwrite=False, backup_workers=1,
            )
            runner = Mock()
            result = PIPELINE.process_downstream(
                {"asr": {"provider": "volcengine"}}, creator, work, root / "works.json",
                None, state_dir, media_dir, runner, PIPELINE.Logger(root / "log.txt", persist=False),
                {}, args, {"aweme_id": "123", "title": "标题", "stages": {"corrected": "success"}},
            )
            runner.run.assert_not_called()
            self.assertEqual(result["stages"]["feishu_written_back"], "disabled")
            self.assertEqual(result["stages"]["backup_statuses_written_back"], "disabled")

    def test_parser_accepts_backfill_existing(self):
        args = PIPELINE.build_parser().parse_args(["--backfill-existing"])
        self.assertTrue(args.backfill_existing)

    def test_parser_accepts_feishu_only(self):
        args = PIPELINE.build_parser().parse_args(["--feishu-only"])
        self.assertTrue(args.feishu_only)

    def test_parser_defaults_creator_collection_workers_to_three(self):
        args = PIPELINE.build_parser().parse_args([])
        self.assertEqual(args.collect_workers, 3)

    def test_feishu_only_preserves_persisted_backup_statuses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_dir = root / "media"
            state_dir = root / "state"
            work = {"aweme_id": "123", "create_time": 1}
            creator = {"key": "demo", "creator_name": "??", "works_table_id": "tbl-demo"}
            paths = PIPELINE.artifact_paths(media_dir, "123", "volcengine")
            paths["final"].parent.mkdir(parents=True, exist_ok=True)
            paths["final"].write_text("????", encoding="utf-8")
            state_path = state_dir / "demo" / "123.json"
            state = PIPELINE.load_state(state_path, creator, work)
            PIPELINE.set_status(state, "ima_backed_up", "failed", "????")
            PIPELINE.set_status(state, "kuake_backed_up", "success")
            PIPELINE.set_status(state, "obsidian_exported", "success")
            PIPELINE.write_json(state_path, state)
            args = argparse.Namespace(
                dry_run=False, skip_feishu_writeback=False, skip_ima=False, skip_kuake=False,
                skip_obsidian=False, feishu_only=True, resume=True, force_stage=set(),
                fail_fast=False, overwrite=False, backup_workers=1,
            )
            runner = Mock()
            result = PIPELINE.process_downstream(
                {"asr": {"provider": "volcengine"}}, creator, work, root / "works.json",
                None, state_dir, media_dir, runner, PIPELINE.Logger(root / "log.txt", persist=False),
                {}, args, {"aweme_id": "123", "title": "title", "stages": {"corrected": "disabled"}},
            )
            command = runner.run.call_args.args[1]
            self.assertEqual(result["stages"]["ima_backed_up"], "failed")
            self.assertEqual(result["stages"]["kuake_backed_up"], "skipped")
            self.assertEqual(result["stages"]["obsidian_exported"], "skipped")
            self.assertEqual(command[command.index("--ima-status") + 1], "\u5931\u8d25")
            self.assertEqual(command[command.index("--kuake-status") + 1], "\u5df2\u4e0a\u4f20")
            self.assertEqual(command[command.index("--local-status") + 1], "\u5df2\u5199\u5165")

    def test_disabled_feishu_mapping_marker_is_not_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = root / "ima.json"
            metadata.write_text(json.dumps({
                "platform": "ima",
                "directory": {"name": "??", "path": "/??"},
                "feishu_fields": {"IMA??": "/??"},
            }, ensure_ascii=False), encoding="utf-8")
            marker = root / "feishu-sync.json"
            marker.write_text(json.dumps({
                "creator_key": "demo",
                "signature": PIPELINE.mapping_cache_signature([metadata]),
                "feishu_writeback": "disabled",
            }), encoding="utf-8")
            self.assertFalse(PIPELINE.mapping_cache_is_fresh(
                [metadata], 24, marker_path=marker,
                expected_creator_name={"ima": "??"}, expected_creator_key="demo",
            ))


if __name__ == "__main__":
    unittest.main()
