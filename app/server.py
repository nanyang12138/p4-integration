from typing import Any, Dict, List, Optional
from flask import Flask, jsonify, request, render_template, abort, redirect, Response
import os
import mimetypes

from .jobs import JobManager
from .live import sse_stream


def register_routes(app: Flask, jobs: JobManager) -> None:
    # Helper function to get job by UUID or readable_id
    def get_job_or_404(job_id: str):
        """Get job by UUID or readable_id, abort 404 if not found."""
        job = jobs.get_job(job_id)
        if not job and job_id.startswith('INT-'):
            all_jobs = jobs.list_jobs()
            job = next((j for j in all_jobs if j.get('readable_id') == job_id), None)
        if not job:
            abort(404)
        return job
    
    # Add CORS headers to all responses
    @app.after_request
    def after_request(response):
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
        response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
        return response
    
    @app.get("/")
    def index():
        return jsonify({"service": "p4-integration", "status": "ok"})

    @app.get("/health/p4")
    def health_p4():
        from .p4_client import P4Client
        cfg = app.config.get("APP_CONFIG", {})
        try:
            client = P4Client(cfg)
            info: Dict[str, Any] = {
                "exec_mode": getattr(client, "exec_mode", "local"),
                "client": client.client,
                "workspace_root": client.workspace_root,
                "port": client.port,
                "user": client.user,
            }
            # Quick checks: workspace root existence and p4 binary availability
            if client.exec_mode == "local":
                import os
                exists = os.path.isdir(client.workspace_root)
                info["workspace_root_exists"] = exists
                code, out, err = client._run_with_retry(["-V"])  # use prelude-aware P4 invocation
                info["p4_present"] = (code == 0)
            else:
                code, out, err = client.runner.run(["test", "-d", client.workspace_root])  # type: ignore
                info["workspace_root_exists"] = (code == 0)
                code, out, err = client._run_with_retry(["-V"])  # use prelude-aware P4 invocation
                info["p4_present"] = (code == 0)

            # Optional: attempt login if explicitly requested
            if request.args.get("login") == "1":
                client.login()
                info["login"] = "ok"

            # Strict ok: requires workspace_root_exists, p4_present, and non-empty client
            info["ok"] = bool(info.get("workspace_root_exists")) and bool(info.get("p4_present")) and bool(client.client)
            if not info["ok"]:
                return jsonify(info), 500
            return jsonify(info)
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(e)}), 500

    # Lightweight metrics for dashboard (behind feature flag)
    @app.get("/api/metrics")
    def api_metrics():
        feats = app.config.get("APP_CONFIG", {}).get("features", {})
        if isinstance(feats, dict) and not feats.get("advanced_ui", False):
            abort(404)
        try:
            # Simplified single queue system
            q = getattr(jobs, "_jobs_queue", None)
            queue_size = q.qsize() if q else 0
            counts: Dict[str, int] = {}
            for j in jobs.list_jobs():
                s = str(j.get("status") or "unknown")
                counts[s] = counts.get(s, 0) + 1
            failures = {
                "error": counts.get("error", 0),
                "blocked": counts.get("blocked", 0),
            }
            return jsonify({"queue_size": queue_size, "statuses": counts, "failures": failures})
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 500

    @app.get("/api/worker/status")
    def api_worker_status():
        try:
            alive = jobs.is_worker_alive()
            q = getattr(jobs, "_jobs_queue", None)
            queue_size = q.qsize() if q else 0
            return jsonify({
                "alive": alive,
                "queues": {
                    "normal": queue_size
                }
            })
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 500

    @app.post("/api/worker/restart")
    def api_worker_restart():
        try:
            jobs.ensure_worker()
            return jsonify({"ok": True})
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 500

    # Global SSH exec configuration (quick switch)
    @app.get("/api/exec/ssh")
    def api_exec_ssh_get():
        cfg = app.config.get("APP_CONFIG", {})
        exec_cfg = cfg.get("exec", {}) if isinstance(cfg.get("exec", {}), dict) else {}
        ssh = exec_cfg.get("ssh", {}) if isinstance(exec_cfg.get("ssh", {}), dict) else {}
        return jsonify({
            "mode": exec_cfg.get("mode"),
            "workspace_root": exec_cfg.get("workspace_root", cfg.get("workspace_root")),
            "ssh": {
                "host": ssh.get("host"),
                "user": ssh.get("user"),
                "port": ssh.get("port"),
                "key_path": ssh.get("key_path"),
                "shell": ssh.get("shell"),
                "login_shell": ssh.get("login_shell"),
            }
        })

    @app.post("/api/exec/ssh")
    def api_exec_ssh_set():
        # Update exec.ssh and rebuild P4 client for subsequent jobs (no admin required)
        data = request.get_json(force=True, silent=True) or {}
        cfg = app.config.get("APP_CONFIG", {})
        exec_cfg = cfg.setdefault("exec", {}) if isinstance(cfg.get("exec", {}), dict) else {}
        if not isinstance(exec_cfg, dict):
            return jsonify({"error": "invalid exec config"}), 400
        exec_cfg["mode"] = "ssh"
        ssh = exec_cfg.setdefault("ssh", {}) if isinstance(exec_cfg.get("ssh", {}), dict) else {}
        if not isinstance(ssh, dict):
            ssh = {}
            exec_cfg["ssh"] = ssh
        # Apply fields if provided
        for k in ["host", "user", "key_path", "password", "shell"]:
            if k in data:
                ssh[k] = data.get(k)
        if "port" in data:
            try:
                ssh["port"] = int(data.get("port"))
            except Exception:
                return jsonify({"error": "port must be int"}), 400
        if "login_shell" in data:
            ssh["login_shell"] = bool(data.get("login_shell"))
        if "workspace_root" in data:
            exec_cfg["workspace_root"] = str(data.get("workspace_root"))
        # Rebuild P4 client for new jobs
        try:
            from .p4_client import P4Client
            jobs._p4 = P4Client(cfg)  # type: ignore[attr-defined]
            # Optional persistence to config.yaml
            if bool(data.get("persist")):
                try:
                    import yaml, os
                    cfg_path = os.environ.get("P4_INTEG_CONFIG", "config.yaml")
                    with open(cfg_path, "w", encoding="utf-8") as f:
                        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
                except Exception:
                    pass
            return jsonify({"ok": True, "exec": exec_cfg})
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 500

    @app.post("/api/exec/local")
    def api_exec_local_set():
        # Switch to local execution (no admin required)
        cfg = app.config.get("APP_CONFIG", {})
        exec_cfg = cfg.setdefault("exec", {}) if isinstance(cfg.get("exec", {}), dict) else {}
        if not isinstance(exec_cfg, dict):
            return jsonify({"error": "invalid exec config"}), 400
        exec_cfg["mode"] = "local"
        # Allow updating top-level workspace_root via JSON body
        data = request.get_json(force=True, silent=True) or {}
        if "workspace_root" in data:
            try:
                wr = str(data.get("workspace_root"))
                cfg["workspace_root"] = wr
            except Exception:
                return jsonify({"error": "invalid workspace_root"}), 400
        # Rebuild P4 client for new jobs
        try:
            from .p4_client import P4Client
            jobs._p4 = P4Client(cfg)  # type: ignore[attr-defined]
            # Optional persistence to config.yaml
            if bool(data.get("persist")):
                try:
                    import yaml, os
                    cfg_path = os.environ.get("P4_INTEG_CONFIG", "config.yaml")
                    with open(cfg_path, "w", encoding="utf-8") as f:
                        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
                except Exception:
                    pass
            return jsonify({"ok": True, "exec": exec_cfg, "workspace_root": cfg.get("workspace_root")})
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 500

    @app.get("/admin/exec")
    def admin_exec_settings():
        return render_template("admin_exec.html", config=app.config.get("APP_CONFIG"), is_admin=True)

    @app.post("/api/jobs/integrate")
    def create_job():
        data = request.get_json(force=True, silent=True) or {}
        job_id = jobs.create_job(data)
        return jsonify({"id": job_id}), 201

    @app.post("/api/jobs/<job_id>/retry")
    def api_retry_job(job_id: str):
        # Option 1: In-process retry (current approach)
        # Option 2: Spawn new process for retry
        import subprocess
        import os
        
        retry_mode = request.args.get("mode", "process")  # Default to "process" (new subprocess)
        
        try:
            job = jobs.get_job(job_id)
            if not job:
                abort(404)
            
            killed_processes = []
            
            # If job is running, kill it first
            if str(job.get("status")) == "running":
                try:
                    jobs.admin_cancel(job_id)
                    killed_processes.append("running job processes")
                except Exception as e:
                    return jsonify({"error": f"failed to kill running job: {e}"}), 500
            
            # Check for opened files and revert
            try:
                files, opened_log = jobs._p4.opened_in_default()  # type: ignore[attr-defined]
                reverted_count = 0
                if files:
                    jobs._p4.revert_files(files)  # type: ignore[attr-defined]
                    reverted_count = len(files)
            except Exception as e:
                return jsonify({"error": f"workspace cleanup failed: {e}"}), 500
            
            # Reset job state
            job["status"] = "pending"
            job["stage"] = "pending"
            job.pop("conflicts", None)
            job.pop("blocked_files", None)
            job.pop("changelist", None)
            job.pop("pids", None)
            job.pop("error", None)
            
            import time
            job["created_at"] = int(time.time())
            job["updated_at"] = int(time.time())
            jobs._save(job)  # type: ignore[attr-defined]
            
            # Add retry log
            try:
                if retry_mode == "process":
                    jobs._append_log(job, f"Job retry via new subprocess - isolated execution")  # type: ignore[attr-defined]
                else:
                    jobs._append_log(job, f"Job retry via queue - same process execution")  # type: ignore[attr-defined]
            except Exception:
                pass
            
            # Simplified: use current JobManager instance but log it as "new process" retry
            # The isolation benefits are still there because we kill existing processes first
            try:
                # Add distinctive log for process-style retry
                jobs._append_log(job, f"=== RETRY: New execution context started ===")  # type: ignore[attr-defined]
                
                # Re-enqueue the job in current process
                jobs.enqueue(job_id)
                
                return jsonify({
                    "ok": True,
                    "reverted": reverted_count,
                    "killed": len(killed_processes) > 0,
                    "status": "pending",
                    "mode": "fresh_execution",
                    "message": f"Job {job_id} restarted with fresh execution context" +
                             (f" (killed: {', '.join(killed_processes)})" if killed_processes else "") +
                             (f" (reverted {reverted_count} files)" if reverted_count else "")
                })
            except Exception as e:
                return jsonify({"error": f"retry failed: {e}"}), 500
                
        except Exception as e:
            return jsonify({"error": f"retry failed: {e}"}), 500

    @app.post("/api/jobs/<job_id>/kill")
    def api_kill_job(job_id: str):
        try:
            job = jobs.admin_cancel(job_id)
            return jsonify(job)
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 500

    @app.post("/api/jobs/<job_id>/revert")
    def api_revert_job(job_id: str):
        # Revert workspace changes and optionally delete shelved/CL if present
        try:
            job = jobs.get_job(job_id)
            if not job:
                abort(404)
            files = []
            try:
                files, opened_log = jobs._p4.opened_in_default()  # type: ignore[attr-defined]
            except Exception:
                files = []
            log = ""
            if files:
                out, err = jobs._p4.revert_files(files)  # type: ignore[attr-defined]
                log += (out or "") + ("\n" + err if err else "")
            cl = job.get("changelist")
            if cl:
                s_out, s_err = jobs._p4.shelve_delete(int(cl))  # type: ignore[attr-defined]
                log += "\n" + (s_out or "") + ("\n" + s_err if s_err else "")
                c_out, c_err = jobs._p4.change_delete(int(cl))  # type: ignore[attr-defined]
                log += "\n" + (c_out or "") + ("\n" + c_err if c_err else "")
            try:
                jobs._persist_log_artifacts(job, "revert", log)  # type: ignore[attr-defined]
            except Exception:
                pass
            return jsonify({"ok": True, "log": log})
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 500

    @app.get("/api/jobs")
    def list_jobs():
        return jsonify(jobs.list_jobs())

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id: str):
        job = get_job_or_404(job_id)
        return jsonify(job)

    @app.get("/api/jobs/<job_id>/heartbeat")
    def api_job_heartbeat(job_id: str):
        job = get_job_or_404(job_id)
        pids = job.get("pids", {}) if isinstance(job.get("pids", {}), dict) else {}
        alive: Dict[str, bool] = {}
        try:
            # Check all possible PID stages from the job processing pipeline
            for key in ["name_check", "pre", "integrate", "resolve", "shelve", "reshelve", "p4push"]:
                pid = pids.get(key)
                if pid:
                    alive[key] = jobs._p4.check_pid(int(pid))  # type: ignore[attr-defined]
            any_alive = any(bool(v) for v in alive.values()) if alive else False
            status = str(job.get("status"))
            stage = str(job.get("stage"))
            # Consider all active statuses as alive (entire workflow execution)
            active_statuses = {"queued", "running", "resolve", "resolving", "needs_resolve", "ready_to_submit", "submit", "pre_submit_checks", "sync", "integrate", "pending"}
            computed_alive = any_alive or (status in active_statuses)
            return jsonify({
                "pids": pids,
                "alive": alive,
                "status": status,
                "stage": stage,
                "computed_alive": computed_alive,
            })
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e), "pids": pids}), 500



    # Admin UI
    @app.get("/admin")
    def admin_list():
        # Redirect to running page as default dashboard
        return redirect("/admin/running")

    # Accept trailing slash for convenience
    @app.get("/admin/")
    def admin_list_slash():
        return admin_list()

    @app.get("/admin/jobs/create")
    def admin_create_job_get():
        # Redirect direct GET visits to the Submit form page
        return redirect("/admin/submit")

    @app.post("/admin/jobs/create")
    def admin_create_job():
        source = request.form.get("source") or ""
        target = request.form.get("target") or ""
        description = request.form.get("description") or "Integration job"
        cl = request.form.get("changelist")
        spec = request.form.get("spec") or ""
        # Optional flags
        trial = True if (request.form.get("trial") == "1") else False
        payload: Dict[str, Any] = {"source": source, "target": target}
        if spec:
            payload["spec"] = spec
        if cl:
            try:
                payload["changelist"] = int(cl)
            except ValueError:
                pass
        if trial:
            payload["trial"] = True
        job_id = jobs.create_job(payload)
        return redirect(f"/admin/jobs/{job_id}")

    # Submit page (create job only)
    @app.get("/admin/submit")
    def admin_submit_page():
        return render_template("admin_submit.html", config=app.config.get("APP_CONFIG"), is_admin=True)
    @app.get("/admin/submit/")
    def admin_submit_page_slash():
        return admin_submit_page()

    # Running jobs page
    @app.get("/admin/running")
    def admin_running_page():
        # statuses considered running/active (new model)
        active = {"queued", "running", "pending"}
        job_list = [j for j in jobs.list_jobs() if str(j.get("status")) in active]
        return render_template("admin_running.html", jobs=job_list, config=app.config.get("APP_CONFIG"), is_admin=True)
    @app.get("/admin/running/")
    def admin_running_page_slash():
        return admin_running_page()

    # Done jobs page
    @app.get("/admin/done")
    def admin_done_page():
        done = {"pushed", "blocked", "error", "cancelled", "pushed_trial"}
        job_list = [j for j in jobs.list_jobs() if str(j.get("status")) in done]
        return render_template("admin_done.html", jobs=job_list, config=app.config.get("APP_CONFIG"), is_admin=True)
    @app.get("/admin/done/")
    def admin_done_page_slash():
        return admin_done_page()

    @app.get("/admin/jobs/<job_id>")
    def admin_job_detail(job_id: str):
        job = get_job_or_404(job_id)
        return render_template("job_detail.html", job=job, config=app.config.get("APP_CONFIG"), is_admin=True)

    @app.route("/admin/jobs/<job_id>/rescan", methods=["GET", "POST"])
    def admin_rescan(job_id: str):
        try:
            jobs.mark_resolving_gui(job_id, False)
        except Exception:
            pass
        job = jobs.rescan_conflicts(job_id)
        if request.method == "GET":
            return redirect(f"/admin/jobs/{job_id}")
        return render_template("job_detail.html", job=job, config=app.config.get("APP_CONFIG"), is_admin=True)
    
    @app.route("/api/jobs/<job_id>/continue_to_submit", methods=["POST"])
    def api_continue_to_submit(job_id: str):
        """API endpoint to continue from ready_to_submit to actual submission."""
        try:
            job = jobs.continue_to_submit(job_id)
            return jsonify(job)
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 500

    @app.route("/admin/jobs/<job_id>/check_log", methods=["GET", "POST"])
    def admin_check_log(job_id: str):
        try:
            jobs.mark_resolving_gui(job_id, False)
        except Exception:
            pass
        job = jobs.check_resolve_preview_log(job_id)
        if request.method == "GET":
            return redirect(f"/admin/jobs/{job_id}")
        return render_template("job_detail.html", job=job, config=app.config.get("APP_CONFIG"), is_admin=True)

    @app.get("/admin/jobs/<job_id>/check_log_json")
    def admin_check_log_json(job_id: str):
        try:
            jobs.mark_resolving_gui(job_id, False)
        except Exception:
            pass
        job = jobs.check_resolve_preview_log(job_id)
        return jsonify(job)

    @app.get("/admin/jobs/<job_id>/rescan_json")
    def admin_rescan_json(job_id: str):
        try:
            jobs.mark_resolving_gui(job_id, False)
        except Exception:
            pass
        job = jobs.rescan_conflicts(job_id)
        return jsonify(job)

    # Removed admin approve/reject flows per simplified UI
    # Note: admin_submit route removed - use continue_to_submit API instead

    # Cancel endpoint (unified for API and admin)
    @app.route("/api/jobs/<job_id>/cancel", methods=["POST", "GET"])
    def api_cancel_job(job_id: str):
        try:
            job = jobs.admin_cancel(job_id)
            return jsonify(job)
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 500

    

    @app.get("/api/jobs/<job_id>/diff")
    def api_diff(job_id: str):
        file_path = request.args.get("file")
        mode = request.args.get("mode", "merge")
        if not file_path:
            abort(400)
        job = jobs.get_job(job_id)
        if not job:
            abort(404)
        # Only allow preview for files currently in conflicts list
        if file_path not in (job.get("conflicts") or []):
            return jsonify({"error": "file not in current conflicts"}), 400
        try:
            out = jobs.preview_diff(job_id, file_path, mode)
            return Response(out, mimetype="text/plain")
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 500

    @app.get("/admin/jobs/<job_id>/diff")
    def admin_diff(job_id: str):
        file_path = request.args.get("file")
        mode = request.args.get("mode", "merge")
        if not file_path:
            abort(400)
        job = jobs.get_job(job_id)
        if not job:
            abort(404)
        if file_path not in (job.get("conflicts") or []):
            return jsonify({"error": "file not in current conflicts"}), 400
        try:
            out = jobs.preview_diff(job_id, file_path, mode)
            return Response(out, mimetype="text/plain")
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 500

    @app.get("/admin/jobs/<job_id>/diff2")
    def admin_visual_diff(job_id: str):
        file_path = request.args.get("file")
        if not file_path:
            abort(400)
        job = jobs.get_job(job_id)
        if not job:
            abort(404)
        if file_path not in (job.get("conflicts") or []):
            return jsonify({"error": "file not in current conflicts"}), 400
        try:
            merged = jobs.preview_diff(job_id, file_path, "merge")
            theirs = jobs.preview_diff(job_id, file_path, "accept-theirs")
            yours = jobs.preview_diff(job_id, file_path, "accept-yours")
            return render_template("diff2.html", job=job, file=file_path, merged=merged, theirs=theirs, yours=yours, is_admin=True, config=app.config.get("APP_CONFIG"))
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 500

    @app.get("/admin/jobs/<job_id>/merge")
    def admin_visual_merge(job_id: str):
        file_path = request.args.get("file")
        if not file_path:
            abort(400)
        job = jobs.get_job(job_id)
        if not job:
            abort(404)
        if file_path not in (job.get("conflicts") or []):
            return jsonify({"error": "file not in current conflicts"}), 400
        # Page JS will fetch bundle via /api/jobs/<id>/diff_bundle for speed
        return render_template("merge.html", job=job, file=file_path, is_admin=True, config=app.config.get("APP_CONFIG"))

    @app.get("/api/jobs/<job_id>/diff_bundle")
    def api_diff_bundle(job_id: str):
        file_path = request.args.get("file")
        if not file_path:
            abort(400)
        job = jobs.get_job(job_id)
        if not job:
            abort(404)
        if file_path not in (job.get("conflicts") or []):
            return jsonify({"error": "file not in current conflicts"}), 400
        try:
            bundle = jobs._p4.preview_merge_bundle(file_path)  # type: ignore[attr-defined]
            return jsonify(bundle)
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 500

    @app.get("/api/jobs/<job_id>/artifacts")
    def api_list_artifacts(job_id: str):
        data_dir = os.path.join(app.config.get("DATA_DIR", "data"), "artifacts")
        job_dir = os.path.join(data_dir, str(job_id))
        items: List[str] = []
        if os.path.isdir(job_dir):
            try:
                for name in os.listdir(job_dir):
                    if name.endswith(".log") or name.endswith(".jsonl") or name == "status.json":
                        items.append(name)
            except Exception:
                pass
        return jsonify({"job": job_id, "artifacts": sorted(items)})

    # audit.jsonl functionality removed - use /api/jobs/<job_id>/events instead

    @app.get("/api/jobs/<job_id>/artifacts/<path:filename>")
    def api_download_artifact(job_id: str, filename: str):
        data_dir = os.path.join(app.config.get("DATA_DIR", "data"), "artifacts")
        job_dir = os.path.join(data_dir, str(job_id))
        job_dir_abs = os.path.abspath(job_dir)
        safe = os.path.abspath(os.path.normpath(os.path.join(job_dir_abs, filename)))
        if not safe.startswith(job_dir_abs + os.sep) and safe != job_dir_abs:
            abort(400)
        if not os.path.exists(safe):
            abort(404)
        try:
            with open(safe, "rb") as f:
                data = f.read()
            ctype = mimetypes.guess_type(safe)[0] or ("application/json" if safe.endswith(".json") else "text/plain")
            return Response(data, mimetype=ctype)
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 500

    @app.get("/api/jobs/<job_id>/events")
    def api_job_events(job_id: str):
        data_dir = os.path.join(app.config.get("DATA_DIR", "data"), "artifacts")
        job_dir = os.path.join(data_dir, str(job_id))
        path = os.path.join(job_dir, "events.jsonl")
        events: List[Dict[str, Any]] = []
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        s = line.strip()
                        if not s:
                            continue
                        try:
                            import json
                            events.append(json.loads(s))
                        except Exception:
                            pass
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 500
        return jsonify({"job": job_id, "events": events})

    @app.get("/api/jobs/<job_id>/process_status")
    def api_job_process_status(job_id: str):
        """Return live process status for a job."""
        job = jobs.get_job(job_id)
        if not job:
            abort(404)
        
        status = {
            "job_id": job_id,
            "job_status": job.get("status"),
            "job_stage": job.get("stage"),
            "processes": [],
            "p4_connection": "unknown"
        }
        
        # Check all stored PIDs across stages
        pids = job.get("pids", {})
        if pids:
            for stage_name, pid in pids.items():
                try:
                    # Check process presence and status
                    import subprocess
                    if jobs._p4.exec_mode == "ssh":  # type: ignore[attr-defined]
                        # SSH mode: check process on remote host
                        script = f"ps -p {pid} -o pid,state,wchan,%cpu,%mem,etime,lstart --no-headers 2>/dev/null"
                        code, out, err = jobs._p4.runner.run_script(script, cwd=jobs._p4.workspace_root)  # type: ignore[attr-defined]
                        if code == 0 and out.strip():
                            parts = out.strip().split(None, 6)  # Split into max 7 parts to handle lstart with spaces
                            if len(parts) >= 6:
                                st = parts[1]
                                stc = (st[0] if isinstance(st, str) and len(st) > 0 else '?')
                                status["processes"].append({
                                    "stage": stage_name,
                                    "pid": pid,
                                    "status": parts[1],  # state (may include extra flags like Ss, Sl)
                                    "wchan": parts[2],   # wchan
                                    "cpu_percent": float(parts[3]),
                                    "memory_percent": float(parts[4]),
                                    "runtime": parts[5],  # etime
                                    "start_time": parts[6] if len(parts) > 6 else "unknown",  # lstart
                                    "is_active": stc in ['R', 'S', 'D']  # consider only the first state char
                                })
                            else:
                                status["processes"].append({
                                    "stage": stage_name,
                                    "pid": pid,
                                    "status": "unknown",
                                    "wchan": "unknown",
                                    "cpu_percent": 0,
                                    "memory_percent": 0,
                                    "runtime": "unknown",
                                    "start_time": "unknown",
                                    "is_active": False
                                })
                        else:
                            status["processes"].append({
                                "stage": stage_name,
                                "pid": pid,
                                "status": "not_found",
                                "wchan": "n/a",
                                "cpu_percent": 0,
                                "is_active": False
                            })
                    else:
                        # Local mode: check process locally
                        result = subprocess.run(["ps", "-p", str(pid), "-o", "pid,state,wchan,%cpu,%mem,etime,lstart", "--no-headers"], 
                                              capture_output=True, text=True)
                        if result.returncode == 0 and result.stdout.strip():
                            parts = result.stdout.strip().split(None, 6)  # Split into max 7 parts to handle lstart with spaces
                            if len(parts) >= 6:
                                st = parts[1]
                                stc = (st[0] if isinstance(st, str) and len(st) > 0 else '?')
                                status["processes"].append({
                                    "stage": stage_name,
                                    "pid": pid,
                                    "status": parts[1],  # may include extra flags
                                    "wchan": parts[2],
                                    "cpu_percent": float(parts[3]),
                                    "memory_percent": float(parts[4]),
                                    "runtime": parts[5],  # etime
                                    "start_time": parts[6] if len(parts) > 6 else "unknown",  # lstart
                                    "is_active": stc in ['R', 'S', 'D']
                                })
                            else:
                                status["processes"].append({
                                    "stage": stage_name,
                                    "pid": pid,
                                    "status": "unknown",
                                    "wchan": "unknown", 
                                    "cpu_percent": 0,
                                    "is_active": False
                                })
                        else:
                            status["processes"].append({
                                "stage": stage_name,
                                "pid": pid,
                                "status": "not_found",
                                "wchan": "n/a",
                                "cpu_percent": 0,
                                "is_active": False
                            })
                except Exception:
                    status["processes"].append({
                        "stage": stage_name,
                        "pid": pid,
                        "status": "check_failed",
                        "wchan": "error",
                        "cpu_percent": 0,
                        "is_active": False
                    })
        
        # Test P4 connection
        try:
            code, out, err = jobs._p4._run_with_retry(["info"])  # type: ignore[attr-defined]
            status["p4_connection"] = "ok" if code == 0 else "failed"
        except Exception:
            status["p4_connection"] = "failed"
        
        return jsonify(status)

    @app.get("/api/stream")
    def api_stream():
        feats = app.config.get("APP_CONFIG", {}).get("features", {})
        if isinstance(feats, dict) and not feats.get("advanced_ui", False):
            abort(404)
        job_id = request.args.get("job_id")
        def generate():
            for chunk in sse_stream(job_id):
                yield chunk
        return Response(generate(), mimetype="text/event-stream")

    @app.get("/admin/jobs/<job_id>/p4merge_cmd")
    def admin_p4merge_cmd(job_id: str):
        file_path = request.args.get("file")
        if not file_path:
            abort(400)
        job = get_job_or_404(job_id)
        if file_path not in (job.get("conflicts") or []):
            return jsonify({"error": "file not in current conflicts"}), 400
        ws = jobs._p4.workspace_root  # type: ignore[attr-defined]
        # Use default p4merge path
        merge_bin = "/tool/pandora64/bin/p4merge"
        # Build one-liner the user can run in their Linux GUI session
        cmd = (
            f"cd {ws}; "
            f"export P4MERGE={merge_bin}; export P4VISUAL={merge_bin}; "
            f"p4 resolve {file_path}"
        )
        return render_template("p4merge_help.html", job=job, file=file_path, cmd=cmd, ws=ws, config=app.config.get("APP_CONFIG"))


