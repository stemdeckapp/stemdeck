# Test-coverage gap inventory

State at the time of writing: **95%** statement coverage of
`app/`, with 266 of 5546 statements uncovered
across the Python suite. The frontend is covered separately by `tests/js/`
(unit) and `tests/e2e/` (Playwright); neither is measured here.

## Measuring this

```
COVERAGE_CORE=sysmon python3.12 -m pytest tests/ --cov=app --cov-report=term-missing
```

Use the 3.12 tracer. On Python 3.11 coverage falls back to the C tracer, which
records only the suspend/resume points inside an `async` FastAPI handler and
reports the rest of the body as unreached even when it demonstrably ran. That
understates `app/api/*.py` and `app/main.py` by roughly four points overall and
invents gaps that no test can close, so a `--cov-fail-under` gate configured
from a 3.11 run will be measuring the tracer rather than the suite.

ffmpeg must be on PATH (CI installs it); without it the export and click-render
tests fail rather than skip.

## What is left, and why

Two categories, counted per file below.

**Reachable** -- ordinary branches a test could drive today. Mostly validation
rejections, `return None` guards on degenerate input, and HTTP error responses.

**Needs fault injection** -- `except` bodies, kill/terminate paths and their
logging, reached only by making a subprocess hang, a disk fill, or a socket die
mid-read. Each is individually coverable by patching the failure in, but a test
that patches the failure it is asserting on largely restates the code. These are
worth covering where the *handler* does something non-obvious (cleanup, a
fallback, a specific status code) and worth leaving where it only logs.

| File | Cov | Uncovered | Reachable | Needs fault injection |
|---|---:|---:|---:|---:|
| `app/api/stems.py` | 93% | 39 | 23 | 16 |
| `app/pipeline/sections.py` | 89% | 28 | 21 | 7 |
| `app/pipeline/beatgrid.py` | 93% | 25 | 19 | 6 |
| `app/api/jobs.py` | 95% | 23 | 19 | 4 |
| `app/main.py` | 95% | 23 | 14 | 9 |
| `app/pipeline/runner.py` | 91% | 19 | 8 | 11 |
| `app/pipeline/section_refine.py` | 93% | 14 | 14 | 0 |
| `app/pipeline/separate.py` | 92% | 12 | 7 | 5 |
| `app/pipeline/vocal_split.py` | 87% | 11 | 6 | 5 |
| `app/pipeline/download.py` | 96% | 10 | 9 | 1 |
| `app/pipeline/click_render.py` | 96% | 9 | 9 | 0 |
| `app/pipeline/collect.py` | 95% | 9 | 3 | 6 |
| `app/api/search.py` | 94% | 8 | 7 | 1 |
| `app/core/registry.py` | 98% | 5 | 2 | 3 |
| `app/api/queue.py` | 95% | 4 | 4 | 0 |
| `app/pipeline/beat_detect.py` | 93% | 4 | 2 | 2 |
| `app/core/settings.py` | 99% | 3 | 1 | 2 |
| `app/api/events.py` | 95% | 3 | 3 | 0 |
| `app/pipeline/search.py` | 97% | 2 | 2 | 0 |
| `app/pipeline/section_worker.py` | 97% | 2 | 2 | 0 |
| `app/core/model_cache.py` | 96% | 2 | 0 | 2 |
| `app/pipeline/audio_stats.py` | 91% | 2 | 2 | 0 |
| `app/core/config.py` | 99% | 1 | 1 | 0 |
| `app/pipeline/jobqueue.py` | 99% | 1 | 1 | 0 |
| `app/pipeline/analyze.py` | 99% | 1 | 1 | 0 |
| `app/api/playlist.py` | 99% | 1 | 1 | 0 |
| `app/pipeline/preview.py` | 99% | 1 | 1 | 0 |
| `app/core/models.py` | 99% | 1 | 1 | 0 |
| `app/pipeline/demucs_worker.py` | 98% | 1 | 1 | 0 |
| `app/pipeline/warmup.py` | 97% | 1 | 1 | 0 |
| `app/pipeline/vocal_split_worker.py` | 97% | 1 | 1 | 0 |
| **total** | **95%** | **266** | **186** | **80** |

## Highest-value remaining work

Ranked by what a bug there would cost a user, not by line count.

1. **`app/api/stems.py` export rendering** (~1236-1402) -- the multi-lane ffmpeg
   graph, its click-lane insertion and its failure tail. An error here produces a
   wrong or truncated export rather than an exception.
2. **`app/pipeline/sections.py` workspace lifecycle** -- linking stems into the
   work dir and cleaning it up. Leaks disk per job when wrong.
3. **`app/pipeline/runner.py` stage orchestration** (~209-241) -- stem records,
   mix URLs and the beat-grid stage's own error containment.
4. **`app/pipeline/beatgrid.py` refinement interior** (~227-278) -- onset snapping
   inside `_refine_beats`; drives grid accuracy, is entirely numeric, and is
   testable with synthetic envelopes the way `tests/test_beatgrid_helpers.py`
   already does for the surrounding helpers.
5. **`app/api/jobs.py` vocal-split endpoint tail** -- the running/failed
   transitions around an on-demand split.

## Full line listing

Regenerate with the command above; `--cov-report=term-missing` prints the same
line numbers. Reproduced here so the gaps are reviewable without a run.

### `app/api/stems.py` -- 93%, 39 uncovered

```
  271|         return None
     ...
  315|             except Exception:
  316|                 logger.exception("count-in render failed for %s", job_id)
  317|                 return None
     ...
  319|                 return None
     ...
  338|         except Exception:
  339|             logger.exception("click render failed for %s", job_id)
  340|             return None
     ...
  342|             return None
     ...
  561|         selected.add("vocals")
     ...
  656|             proc.kill()
     ...
  660|         except (TimeoutError, asyncio.TimeoutError):
  661|             drain_task.cancel()
     ...
  674|                 tmp_path.unlink(missing_ok=True)
     ...
  723|         except (TimeoutError, asyncio.TimeoutError):
  724|             proc.kill()
  725|             await proc.wait()
     ...
  728|         except (TimeoutError, asyncio.TimeoutError):
  729|             drain_task.cancel()
     ...
  743|             proc.kill()
  744|             await proc.wait()
     ...
  764|     except OSError:
  765|         logger.debug("could not remove rendered export %s", path, exc_info=True)
     ...
  884|         raise HTTPException(
     ...
  931|         raise HTTPException(
     ...
  940|         cached = await _ensure_cached_mp3(path)
  941|         return FileResponse(
     ...
 1236|         raise HTTPException(
     ...
 1251|         paths = [*paths, click_lane[0]]
 1252|         render_lanes = [*render_lanes, _RenderLane(click_lane[0], click_lane[1], 0)]
     ...
 1271|         out_label = "[a0]"
     ...
 1343|                 tail = proc.stderr[-2000:].decode("utf-8", "replace")
 1344|                 raise RuntimeError(f"ffmpeg failed for {name}: {tail}")
     ...
 1373|             raise HTTPException(
     ...
 1384|         raise HTTPException(status_code=404, detail="stems not found")
     ...
 1399|     except Exception:
 1400|         tmp_path.unlink(missing_ok=True)
 1401|         logger.exception("failed to build stems zip for job %s", job_id)
 1402|         raise HTTPException(status_code=500, detail="failed to build archive") from None
```

### `app/pipeline/sections.py` -- 89%, 28 uncovered

```
  189|             return []
     ...
  205|         return
     ...
  209|     except subprocess.TimeoutExpired:
  210|         proc.kill()
  211|         proc.wait(timeout=5)
     ...
  233|         _terminate(proc)
  234|         raise RuntimeError("section-analysis process has no output pipes")
     ...
  243|             with output_lock:
  244|                 sink.append(line.rstrip())
  245|                 last_output[0] = time.monotonic()
     ...
  269|                 logger.warning(
     ...
  274|                 _terminate(proc)
  275|                 break
     ...
  283|         raise JobCancelled()
     ...
  349|     except OSError:
  350|         pass
  351|     try:
  352|         target.symlink_to(source.resolve())
  353|         return
  354|     except OSError:
  355|         pass
  356|     shutil.copy2(source, target)
     ...
  398|         except OSError:
  399|             continue
     ...
  431|             return None
     ...
  434|             return None
     ...
  437|             logger.info("section model produced no valid structure for job %s", job.id)
  438|             return None
```

### `app/pipeline/beatgrid.py` -- 93%, 25 uncovered

```
  176|         return []
     ...
  227|         return coarse, 0
     ...
  231|         return coarse, 0
     ...
  252|             continue
     ...
  271|             continue
     ...
  278|         return coarse, len(refined_idx)
     ...
  333|         return beats, 0
     ...
  351|         out[-1] = last_pred
  352|         corrected += 1
     ...
  500|         return []
     ...
  558|         return beats, 0, 0
     ...
  614|     except ImportError:
  615|         logger.warning("librosa not installed -- skipping beat grid")
  616|         return None
     ...
  631|             return None
     ...
  653|             return None
     ...
  681|             logger.warning("beatgrid: post-processing left only %d beats -- discarding", len(bea
  682|             return None
     ...
  687|             return None
     ...
  690|             logger.warning("beatgrid: implausible tempo %.1f BPM -- discarding", bpm)
  691|             return None
     ...
  712|             return None
     ...
  785|     except Exception:
  786|         logger.exception("beatgrid failed for %s", stems_dir)
  787|         return None
```

### `app/api/jobs.py` -- 95%, 23 uncovered

```
  198|     except Exception as e:
  199|         raise HTTPException(status_code=422, detail=str(e)) from e
     ...
  208|         selected = list(STEM_NAMES)
     ...
  249|         raise HTTPException(status_code=422, detail="No file provided")
     ...
  275|         raise HTTPException(status_code=422, detail="File exceeds 400 MB limit")
     ...
  313|         shutil.rmtree(job_dir, ignore_errors=True)
  314|         raise HTTPException(status_code=503, detail=_UPLOAD_QUEUE_FULL_DETAIL)
     ...
  405|         proc = registry_get_proc(job_id)
  406|         if proc is not None and proc.poll() is None:
  407|             proc.terminate()
     ...
  430|         raise HTTPException(status_code=404, detail="job not found")
     ...
  441|         raise HTTPException(status_code=404, detail="job not found")
     ...
  467|         job.stem_presence = {**(job.stem_presence or {}), **extra_presence}
     ...
  526|             raise ValueError("time out of range")
     ...
  557|         raise HTTPException(status_code=404, detail="job not found")
     ...
  564|                 meta = json.loads(meta_path.read_text(encoding="utf-8"))
     ...
  617|         raise HTTPException(status_code=404, detail="job not found")
     ...
  649|         raise HTTPException(status_code=404, detail="job not found")
     ...
  656|     except OSError as exc:
  657|         logger.exception("unreadable failure evidence for %s", job_id)
  658|         raise HTTPException(status_code=404, detail="no failure evidence for this job") from exc
     ...
  815|         raise HTTPException(status_code=404, detail="job not found")
     ...
  818|         raise HTTPException(status_code=404, detail="job not found")
```

### `app/main.py` -- 95%, 23 uncovered

```
  109| except ImportError:
  110|     pass
     ...
  210|     except Exception:
  211|         _log.exception("could not sweep orphaned section workspaces")
     ...
  215|         jobqueue.pause()
  216|         for job_id in resumed:
  217|             jobqueue.enqueue(job_id, autostart=False)
  218|         _log.info(
     ...
  225|             try:
  226|                 parent_pid_int = int(parent_pid)
  227|             except ValueError:
  228|                 _log.warning("invalid STEMDECK_PARENT_PID=%r", parent_pid)
     ...
  230|                 if parent_pid_int > 0 and parent_pid_int != os.getpid():
  231|                     wt = asyncio.create_task(_desktop_parent_watchdog(parent_pid_int))
  232|                     _background_tasks.add(wt)
  233|                     wt.add_done_callback(_background_tasks.discard)
     ...
  238|         await tls_listener.start(app, port=HTTPS_PORT, certfile=SSL_CERTFILE, keyfile=SSL_KEYFIL
     ...
  333|         return False
     ...
  444|         except (TypeError, OSError):
  445|             raise HTTPException(status_code=422, detail="invalid cookies file") from None
     ...
  627|         _log.warning("reset left %d entries on disk: %s", len(undeleted), undeleted)
     ...
  726|         except ValueError:
  727|             return None
```

### `app/pipeline/runner.py` -- 91%, 19 uncovered

```
   38|     except FileNotFoundError:
   39|         pass
   40|     except Exception:
   41|         logger.warning("failed to remove %s", path, exc_info=True)
     ...
   68|         except subprocess.TimeoutExpired:
   69|             proc.kill()
   70|             proc.communicate()
   71|             raise
     ...
  209|         job.stems.insert(
     ...
  219|         job.mix_url = f"/api/jobs/{job.id}/stems/{mix_path.name}"
     ...
  224|         all_stem_names.append(mix_path.stem)
     ...
  240|     except Exception:
  241|         logger.exception("beat grid stage failed for job %s", job.id)
     ...
  308|     except OSError:
  309|         logger.warning("could not write metadata.json for job %s", job.id, exc_info=True)
     ...
  383|             shutil.rmtree(dest, ignore_errors=True)
     ...
  386|     except Exception:
  387|         logger.warning("[%s] quarantine failed; removing job dir", job.id, exc_info=True)
  388|         _rmtree(job_dir)
```

### `app/pipeline/section_refine.py` -- 93%, 14 uncovered

```
  136|         return None
     ...
  157|         return []
     ...
  170|     return len(spans) - 1
     ...
  187|     return str(spans[_span_index(spans, midpoint)]["label"])
     ...
  202|         return _NEUTRAL_LABEL, 0.0
     ...
  237|                 continue
     ...
  240|                 continue
     ...
  248|             continue
     ...
  297|             continue
     ...
  302|             continue
     ...
  307|             continue
     ...
  309|             continue
     ...
  314|         return fallback
     ...
  320|             label, margin = _original_label(spans, midpoint), 1.0
```

### `app/pipeline/separate.py` -- 92%, 12 uncovered

```
   49|         except subprocess.TimeoutExpired:
   50|             proc.kill()
     ...
   55|             proc.communicate()
     ...
   83|     except ModuleNotFoundError:
   84|         pass
     ...
  155|             if proc.poll() is not None:
  156|                 return
  157|             if time.monotonic() - last_output[0] > TIMEOUT_DEMUCS_STALL:
  158|                 logger.warning(
     ...
  163|                 proc.terminate()
  164|                 return
     ...
  302|         raise SeparationError(f"demucs output not found at {stems_root}", device=job.compute_dev
```

### `app/pipeline/vocal_split.py` -- 87%, 11 uncovered

```
   74|     except ModuleNotFoundError:
   75|         pass
     ...
  100|             if proc.poll() is not None:
  101|                 return
  102|             if time.monotonic() - last_output[0] > TIMEOUT_VOCAL_SPLIT:
  103|                 logger.warning(
     ...
  108|                 proc.terminate()
  109|                 return
     ...
  143|             except subprocess.TimeoutExpired:
  144|                 proc.kill()
  145|                 proc.communicate()
```

### `app/pipeline/download.py` -- 96%, 10 uncovered

```
  217|         opts["ffmpeg_location"] = str(FFMPEG_DIR)
     ...
  226|         opts["cookiefile"] = cookies
     ...
  254|     except Exception as e:
  255|         raise InvalidYouTubeURL(f"could not parse URL: {e}") from e
     ...
  327|                 break
     ...
  447|             raise JobCancelled()
     ...
  479|         raise
     ...
  482|             raise JobCancelled() from exc
     ...
  494|             f.unlink(missing_ok=True)
     ...
  557|         info: dict = _with_retries(job, lambda: _fetch(True), what="download")
```

### `app/pipeline/click_render.py` -- 96%, 9 uncovered

```
   70|         return []
     ...
   90|         return []
     ...
  160|             break
     ...
  165|         return None
     ...
  221|             break
     ...
  247|         return None
     ...
  412|             continue
     ...
  451|         multiplier = 1.0
     ...
  483|         multiplier = 1.0
```

### `app/pipeline/collect.py` -- 95%, 9 uncovered

```
  208|                 continue
     ...
  219|         except Exception:
  220|             logger.warning("could not write peaks.json for %s", stems_dir.name, exc_info=True)
     ...
  238|         try:
  239|             peaks = json.loads(path.read_text(encoding="utf-8"))
  240|         except (OSError, json.JSONDecodeError):
  241|             logger.warning("could not read existing peaks.json in %s", stems_dir, exc_info=True)
     ...
  259|     except Exception:
  260|         logger.warning("could not write peaks.json for %s", stems_dir.name, exc_info=True)
```

### `app/api/search.py` -- 94%, 8 uncovered

```
   78|         _cache.pop(key, None)
   79|         return None
     ...
  139|                 return {**cached, "cached": True}
     ...
  144|         raise HTTPException(status_code=422, detail=str(e)) from e
     ...
  207|                 yield chunk
  208|         except (BrokenPipeError, ConnectionResetError, GeneratorExit):
     ...
  210|             return
     ...
  236|         raise HTTPException(status_code=502, detail="Could not load a preview") from e
```

### `app/core/registry.py` -- 98%, 5 uncovered

```
  210|                     continue
     ...
  334|         except (OSError, json.JSONDecodeError):
  335|             pass
     ...
  345|         except OSError:
  346|             logger.warning("could not write recovery metadata for %s", job_dir.name, exc_info=Tr
```

### `app/api/queue.py` -- 95%, 4 uncovered

```
  107|         raise HTTPException(status_code=404, detail="job not found")
     ...
  149|                         continue
     ...
  155|                     yield ": keepalive\n\n"
  156|                     keepalive_at = 0
```

### `app/pipeline/beat_detect.py` -- 93%, 4 uncovered

```
   61|             return _model
     ...
   76|             logger.info("beat model loaded (checkpoint=%s)", BEAT_MODEL_CHECKPOINT)
     ...
   78|             logger.info("beat_this not installed -- using librosa beat tracking")
   79|             _model_failed = True
```

### `app/core/settings.py` -- 99%, 3 uncovered

```
  135|     except OSError:
  136|         _log.warning("could not move unreadable settings at %s aside", path, exc_info=True)
     ...
  193|         return False
```

### `app/api/events.py` -- 95%, 3 uncovered

```
  117|                     return
     ...
  120|                     yield ": keepalive\n\n"
  121|                     keepalive_at = 0
```

### `app/pipeline/search.py` -- 97%, 2 uncovered

```
  108|         return entry["thumbnail"]
     ...
  116|     return thumbs[-1]["url"]
```

### `app/pipeline/section_worker.py` -- 97%, 2 uncovered

```
   38|         print("SECTION_HEARTBEAT", file=sys.stderr, flush=True)
     ...
  132|     sys.exit(main())
```

### `app/core/model_cache.py` -- 96%, 2 uncovered

```
   54|     except OSError:
     ...
   57|         logger.warning("could not remove cached model artifact %s", path, exc_info=True)
```

### `app/pipeline/audio_stats.py` -- 91%, 2 uncovered

```
   23|         return [], 0.0
     ...
   37|             continue
```

### `app/core/config.py` -- 99%, 1 uncovered

```
  338|     BEAT_DETECTOR = "auto"
```

### `app/pipeline/jobqueue.py` -- 99%, 1 uncovered

```
   56|         return
```

### `app/pipeline/analyze.py` -- 99%, 1 uncovered

```
  224|         return None
```

### `app/api/playlist.py` -- 99%, 1 uncovered

```
  133|         selected = list(STEM_NAMES)
```

### `app/pipeline/preview.py` -- 99%, 1 uncovered

```
   90|             continue
```

### `app/core/models.py` -- 99%, 1 uncovered

```
  192|             raise ValueError("job record missing id")
```

### `app/pipeline/demucs_worker.py` -- 98%, 1 uncovered

```
  115|     main()
```

### `app/pipeline/warmup.py` -- 97%, 1 uncovered

```
  110|     sys.exit(main())
```

### `app/pipeline/vocal_split_worker.py` -- 97%, 1 uncovered

```
   89|     main()
```

