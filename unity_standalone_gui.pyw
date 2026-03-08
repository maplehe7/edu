from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText


BASE_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = BASE_DIR / "unity_standalone.py"
FINDER_SCRIPT_PATH = BASE_DIR / "unity_standalone_finder.py"
OUTPUT_DIR_PATTERN = re.compile(r'"output_dir"\s*:\s*"([^"]+)"')
FINDER_RESULT_PREFIX = "[finder-result] "
DEFAULT_SIZE = "980x760"


class UnityStandaloneGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Unity Standalone Builder")
        self.geometry(DEFAULT_SIZE)
        self.minsize(860, 620)

        self.mode_var = tk.StringVar(value="entry")
        self.game_name_var = tk.StringVar()
        self.candidate_url_var = tk.StringVar()
        self.candidate_summary_var = tk.StringVar(value="Search results will appear here.")
        self.entry_url_var = tk.StringVar()
        self.output_dir_var = tk.StringVar()
        self.loader_url_var = tk.StringVar()
        self.framework_url_var = tk.StringVar()
        self.data_url_var = tk.StringVar()
        self.wasm_url_var = tk.StringVar()
        self.overwrite_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Idle")

        self.process: subprocess.Popen[str] | None = None
        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.last_output_dir = ""
        self.last_finder_result: dict[str, object] | None = None
        self.finder_candidates: list[dict[str, object]] = []
        self.finder_candidate_index = -1
        self.current_action = ""
        self.locked_widgets: list[tk.Widget] = []

        self._build_ui()
        self._sync_candidate_controls()
        self._sync_mode()
        self.protocol("WM_DELETE_WINDOW", self._handle_close)
        self.after(100, self._poll_events)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        header = ttk.Frame(self, padding=(16, 16, 16, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        ttk.Label(
            header,
            text="Unity Standalone Builder",
            font=("Segoe UI", 17, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="Search by game name, entry URL, or direct Unity asset URLs.",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        form = ttk.Frame(self, padding=(16, 0, 16, 8))
        form.grid(row=1, column=0, sticky="nsew")
        form.columnconfigure(0, weight=1)

        mode_frame = ttk.LabelFrame(form, text="Mode", padding=12)
        mode_frame.grid(row=0, column=0, sticky="ew")
        mode_frame.columnconfigure(0, weight=1)
        mode_frame.columnconfigure(1, weight=1)

        entry_radio = ttk.Radiobutton(
            mode_frame,
            text="Entry URL",
            value="entry",
            variable=self.mode_var,
            command=self._sync_mode,
        )
        entry_radio.grid(row=0, column=0, sticky="w")
        direct_radio = ttk.Radiobutton(
            mode_frame,
            text="Direct Unity URLs",
            value="direct",
            variable=self.mode_var,
            command=self._sync_mode,
        )
        direct_radio.grid(row=0, column=1, sticky="w")
        self.locked_widgets.extend([entry_radio, direct_radio])

        self.entry_frame = ttk.LabelFrame(form, text="Entry URL", padding=12)
        self.entry_frame.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.entry_frame.columnconfigure(1, weight=1)
        ttk.Label(self.entry_frame, text="Game name").grid(
            row=0,
            column=0,
            sticky="w",
            padx=(0, 10),
            pady=4,
        )
        game_name_entry = ttk.Entry(self.entry_frame, textvariable=self.game_name_var)
        game_name_entry.grid(row=0, column=1, sticky="ew", pady=4)
        self.find_button = ttk.Button(
            self.entry_frame,
            text="Find Best URL",
            command=self.start_find,
        )
        self.find_button.grid(row=0, column=2, sticky="w", padx=(8, 0), pady=4)
        ttk.Label(
            self.entry_frame,
            text="Search the web and pick the best supported source for the builder.",
        ).grid(row=1, column=1, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Separator(self.entry_frame, orient="horizontal").grid(
            row=2,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=(2, 8),
        )
        self.locked_widgets.extend([game_name_entry, self.find_button])
        candidate_frame = ttk.LabelFrame(self.entry_frame, text="Finder Candidate", padding=10)
        candidate_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        candidate_frame.columnconfigure(0, weight=1)
        candidate_url_entry = ttk.Entry(
            candidate_frame,
            textvariable=self.candidate_url_var,
            state="readonly",
        )
        candidate_url_entry.grid(row=0, column=0, columnspan=4, sticky="ew")
        ttk.Label(
            candidate_frame,
            textvariable=self.candidate_summary_var,
            wraplength=760,
            justify="left",
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 8))
        self.prev_candidate_button = ttk.Button(
            candidate_frame,
            text="Previous",
            command=self.show_previous_candidate,
        )
        self.prev_candidate_button.grid(row=2, column=0, sticky="w")
        self.open_candidate_button = ttk.Button(
            candidate_frame,
            text="Open",
            command=self.open_candidate_link,
        )
        self.open_candidate_button.grid(row=2, column=1, sticky="w", padx=(8, 0))
        self.next_candidate_button = ttk.Button(
            candidate_frame,
            text="Next",
            command=self.show_next_candidate,
        )
        self.next_candidate_button.grid(row=2, column=2, sticky="w", padx=(8, 0))
        self.accept_candidate_button = ttk.Button(
            candidate_frame,
            text="Accept",
            command=self.accept_candidate,
        )
        self.accept_candidate_button.grid(row=2, column=3, sticky="e", padx=(8, 0))
        self._add_labeled_entry(
            self.entry_frame,
            row=4,
            label="Game URL",
            variable=self.entry_url_var,
            example="https://melonplayground.io/",
        )

        self.direct_frame = ttk.LabelFrame(form, text="Direct Unity URLs", padding=12)
        self.direct_frame.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.direct_frame.columnconfigure(1, weight=1)
        self._add_labeled_entry(
            self.direct_frame,
            row=0,
            label="Loader URL",
            variable=self.loader_url_var,
        )
        self._add_labeled_entry(
            self.direct_frame,
            row=1,
            label="Framework URL",
            variable=self.framework_url_var,
        )
        self._add_labeled_entry(
            self.direct_frame,
            row=2,
            label="Data URL",
            variable=self.data_url_var,
        )
        self._add_labeled_entry(
            self.direct_frame,
            row=3,
            label="Wasm URL",
            variable=self.wasm_url_var,
        )

        options_frame = ttk.LabelFrame(form, text="Options", padding=12)
        options_frame.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        options_frame.columnconfigure(1, weight=1)

        self._add_labeled_entry(
            options_frame,
            row=0,
            label="Output folder",
            variable=self.output_dir_var,
            example="Optional. Leave blank to infer from the game.",
        )

        overwrite_check = ttk.Checkbutton(
            options_frame,
            text="Overwrite existing output folder",
            variable=self.overwrite_var,
        )
        overwrite_check.grid(row=2, column=1, sticky="w", pady=(8, 0))
        self.locked_widgets.append(overwrite_check)

        actions = ttk.Frame(form, padding=(0, 12, 0, 0))
        actions.grid(row=4, column=0, sticky="ew")
        actions.columnconfigure(5, weight=1)

        self.build_button = ttk.Button(actions, text="Build", command=self.start_build)
        self.build_button.grid(row=0, column=0, sticky="w")

        self.stop_button = ttk.Button(actions, text="Stop", command=self.stop_build, state="disabled")
        self.stop_button.grid(row=0, column=1, sticky="w", padx=(8, 0))

        clear_button = ttk.Button(actions, text="Clear Log", command=self.clear_log)
        clear_button.grid(row=0, column=2, sticky="w", padx=(8, 0))

        open_button = ttk.Button(actions, text="Open Output Folder", command=self.open_output_folder)
        open_button.grid(row=0, column=3, sticky="w", padx=(8, 0))

        ttk.Label(actions, textvariable=self.status_var).grid(row=0, column=5, sticky="e")

        log_frame = ttk.LabelFrame(self, text="Build Log", padding=(12, 10, 12, 12))
        log_frame.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 16))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = ScrolledText(
            log_frame,
            wrap="word",
            font=("Consolas", 10),
            height=18,
            state="disabled",
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")

        self.locked_widgets.extend([self.build_button])

    def _add_labeled_entry(
        self,
        parent: ttk.LabelFrame,
        row: int,
        label: str,
        variable: tk.StringVar,
        example: str = "",
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        self.locked_widgets.append(entry)
        if example:
            ttk.Label(parent, text=example).grid(row=row + 1, column=1, sticky="w", pady=(0, 4))

    def _sync_mode(self) -> None:
        if self.mode_var.get() == "entry":
            self.entry_frame.grid()
            self.direct_frame.grid_remove()
        else:
            self.entry_frame.grid_remove()
            self.direct_frame.grid()

    def _set_running_state(self, is_running: bool) -> None:
        for widget in self.locked_widgets:
            try:
                widget.configure(state="disabled" if is_running else "normal")
            except tk.TclError:
                continue
        self.build_button.configure(state="disabled" if is_running else "normal")
        self.stop_button.configure(state="normal" if is_running else "disabled")
        if is_running:
            for button in (
                self.prev_candidate_button,
                self.open_candidate_button,
                self.next_candidate_button,
                self.accept_candidate_button,
            ):
                button.configure(state="disabled")
        else:
            self._sync_candidate_controls()

    def append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

        for match in OUTPUT_DIR_PATTERN.finditer(text):
            self.last_output_dir = bytes(match.group(1), "utf-8").decode("unicode_escape")

        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith(FINDER_RESULT_PREFIX):
                continue
            try:
                payload = json.loads(stripped[len(FINDER_RESULT_PREFIX) :])
            except json.JSONDecodeError:
                continue
            self._apply_finder_result(payload)

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def build_command(self) -> list[str]:
        command = [sys.executable, "-u", str(SCRIPT_PATH)]

        if self.mode_var.get() == "entry":
            entry_url = self.entry_url_var.get().strip()
            if not entry_url:
                raise ValueError("Entry URL is required.")
            command.append(entry_url)
        else:
            direct_fields = {
                "--loader-url": self.loader_url_var.get().strip(),
                "--framework-url": self.framework_url_var.get().strip(),
                "--data-url": self.data_url_var.get().strip(),
                "--wasm-url": self.wasm_url_var.get().strip(),
            }
            missing = [flag for flag, value in direct_fields.items() if not value]
            if missing:
                raise ValueError("All direct Unity URLs are required in Direct Unity URLs mode.")
            for flag, value in direct_fields.items():
                command.extend([flag, value])

        output_dir = self.output_dir_var.get().strip()
        if output_dir:
            command.extend(["--out", output_dir])

        if self.overwrite_var.get():
            command.append("--overwrite")

        return command

    def finder_command(self) -> list[str]:
        game_name = self.game_name_var.get().strip()
        if not game_name:
            raise ValueError("Game name is required.")
        return [sys.executable, "-u", str(FINDER_SCRIPT_PATH), game_name]

    def _apply_finder_result(self, payload: dict[str, object]) -> None:
        self.last_finder_result = payload
        candidates = payload.get("top_candidates")
        if isinstance(candidates, list):
            self.finder_candidates = [item for item in candidates if isinstance(item, dict)]
        else:
            self.finder_candidates = []
        self.finder_candidate_index = 0 if self.finder_candidates else -1
        self._refresh_candidate_preview()

    def _selected_candidate(self) -> dict[str, object] | None:
        if self.finder_candidate_index < 0:
            return None
        if self.finder_candidate_index >= len(self.finder_candidates):
            return None
        return self.finder_candidates[self.finder_candidate_index]

    def _refresh_candidate_preview(self) -> None:
        candidate = self._selected_candidate()
        if candidate is None:
            self.candidate_url_var.set("")
            self.candidate_summary_var.set("Search results will appear here.")
            self._sync_candidate_controls()
            return

        self.candidate_url_var.set(str(candidate.get("source_url") or ""))
        position = f"{self.finder_candidate_index + 1}/{len(self.finder_candidates)}"
        confidence_label = str(candidate.get("confidence_label") or "").strip()
        confidence_value = str(candidate.get("confidence") or "").strip()
        summary = position
        if confidence_label:
            summary += f"  {confidence_label}"
            if confidence_value:
                summary += f" ({confidence_value})"
        summary += f"  {candidate.get('entry_kind') or 'unknown'}"
        build_kind = str(candidate.get("build_kind") or "").strip()
        if build_kind:
            summary += f" / {build_kind}"
        compatibility_summary = str(candidate.get("compatibility_summary") or "").strip()
        if compatibility_summary:
            summary += f"  {compatibility_summary}"
        school_network_summary = str(candidate.get("school_network_summary") or "").strip()
        school_network_risk_label = str(candidate.get("school_network_risk_label") or "").strip()
        school_network_risk = str(candidate.get("school_network_risk") or "").strip()
        if school_network_risk_label:
            summary += f"  School: {school_network_risk_label}"
            if school_network_risk:
                summary += f" ({school_network_risk})"
            if school_network_risk_label != "Low" and school_network_summary:
                summary += f" {school_network_summary}"
        resolved_entry_url = str(candidate.get("resolved_entry_url") or "").strip()
        if resolved_entry_url:
            summary += f"  ->  {resolved_entry_url}"
        reason = str(candidate.get("reason") or "").strip()
        if reason:
            summary += f"  [{reason}]"
        self.candidate_summary_var.set(summary)
        self._sync_candidate_controls()

    def _sync_candidate_controls(self) -> None:
        has_candidate = self._selected_candidate() is not None
        prev_state = "normal" if has_candidate and self.finder_candidate_index > 0 else "disabled"
        next_state = (
            "normal"
            if has_candidate and self.finder_candidate_index < len(self.finder_candidates) - 1
            else "disabled"
        )
        shared_state = "normal" if has_candidate else "disabled"
        self.prev_candidate_button.configure(state=prev_state)
        self.next_candidate_button.configure(state=next_state)
        self.open_candidate_button.configure(state=shared_state)
        self.accept_candidate_button.configure(state=shared_state)

    def show_previous_candidate(self) -> None:
        if self.finder_candidate_index <= 0:
            return
        self.finder_candidate_index -= 1
        self._refresh_candidate_preview()

    def show_next_candidate(self) -> None:
        if self.finder_candidate_index >= len(self.finder_candidates) - 1:
            return
        self.finder_candidate_index += 1
        self._refresh_candidate_preview()

    def open_candidate_link(self) -> None:
        candidate = self._selected_candidate()
        if candidate is None:
            return
        url = str(candidate.get("source_url") or "").strip()
        if url:
            webbrowser.open(url)

    def accept_candidate(self) -> None:
        candidate = self._selected_candidate()
        if candidate is None:
            return
        url = str(candidate.get("source_url") or "").strip()
        if not url:
            return
        self.mode_var.set("entry")
        self._sync_mode()
        self.entry_url_var.set(url)
        if not self.output_dir_var.get().strip():
            suggested_output_name = str(candidate.get("suggested_output_name") or "").strip()
            if suggested_output_name:
                self.output_dir_var.set(suggested_output_name)
        self.status_var.set("Candidate accepted")
        self.append_log(f"[gui] Accepted finder candidate: {url}\n")

    def _start_process(self, command: list[str], action_name: str) -> None:
        if action_name == "find":
            self.last_finder_result = None
            self.finder_candidates = []
            self.finder_candidate_index = -1
            self._refresh_candidate_preview()
        self.current_action = action_name
        action_label = "search" if action_name == "find" else action_name
        self.status_var.set(f"Starting {action_label}...")
        self.append_log("$ " + self._format_command(command) + "\n")
        self._set_running_state(True)

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.process = subprocess.Popen(
                command,
                cwd=str(BASE_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except OSError as exc:
            self.process = None
            self.current_action = ""
            self._set_running_state(False)
            self.status_var.set("Failed to start")
            messagebox.showerror("Launch Error", str(exc))
            return

        threading.Thread(target=self._read_process_output, daemon=True).start()

    def start_build(self) -> None:
        if self.process is not None:
            return
        if not SCRIPT_PATH.exists():
            messagebox.showerror("Missing Script", f"Could not find {SCRIPT_PATH}")
            return

        try:
            command = self.build_command()
        except ValueError as exc:
            messagebox.showerror("Missing Input", str(exc))
            return
        self.last_output_dir = ""
        self._start_process(command, "build")

    def start_find(self) -> None:
        if self.process is not None:
            return
        if not FINDER_SCRIPT_PATH.exists():
            messagebox.showerror("Missing Script", f"Could not find {FINDER_SCRIPT_PATH}")
            return

        try:
            command = self.finder_command()
        except ValueError as exc:
            messagebox.showerror("Missing Input", str(exc))
            return
        self._start_process(command, "find")

    def stop_build(self) -> None:
        if self.process is None:
            return
        self.status_var.set("Stopping...")
        self.append_log("[gui] Stopping current action...\n")
        process = self.process
        process.terminate()
        threading.Thread(target=self._force_kill_if_needed, args=(process,), daemon=True).start()

    def open_output_folder(self) -> None:
        candidate = self.last_output_dir or self.output_dir_var.get().strip()
        if not candidate:
            candidate = str(BASE_DIR)
        path = Path(candidate)
        if not path.is_absolute():
            path = (BASE_DIR / path).resolve()
        if not path.exists():
            messagebox.showinfo("Folder Missing", f"Folder does not exist yet:\n{path}")
            return
        os.startfile(path)

    def _read_process_output(self) -> None:
        assert self.process is not None
        process = self.process
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    self.event_queue.put(("log", line))
        finally:
            return_code = process.wait()
            self.event_queue.put(("done", return_code))

    def _force_kill_if_needed(self, process: subprocess.Popen[str]) -> None:
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()

    def _poll_events(self) -> None:
        while True:
            try:
                event_type, payload = self.event_queue.get_nowait()
            except queue.Empty:
                break

            if event_type == "log":
                self.append_log(str(payload))
            elif event_type == "done":
                return_code = int(payload)
                action_name = self.current_action
                self.process = None
                self.current_action = ""
                self._set_running_state(False)
                if return_code == 0:
                    if action_name == "find":
                        if self.last_finder_result is not None:
                            self.status_var.set("Source found")
                            self.append_log("[gui] Search complete. Review candidates and click Accept.\n")
                        else:
                            self.status_var.set("Search complete")
                            self.append_log("[gui] Search complete.\n")
                    else:
                        self.status_var.set("Build complete")
                        self.append_log("[gui] Build complete.\n")
                else:
                    if action_name == "find":
                        self.status_var.set(f"Search failed ({return_code})")
                        self.append_log(f"[gui] Search failed with exit code {return_code}.\n")
                    else:
                        self.status_var.set(f"Build failed ({return_code})")
                        self.append_log(f"[gui] Build failed with exit code {return_code}.\n")

        self.after(100, self._poll_events)

    def _handle_close(self) -> None:
        if self.process is not None:
            should_close = messagebox.askyesno(
                "Build Running",
                "A build is still running. Stop it and close the GUI?",
            )
            if not should_close:
                return
            self.stop_build()
        self.destroy()

    @staticmethod
    def _format_command(command: list[str]) -> str:
        def quote(part: str) -> str:
            return f'"{part}"' if " " in part else part

        return " ".join(quote(part) for part in command)


def main() -> None:
    app = UnityStandaloneGui()
    app.mainloop()


if __name__ == "__main__":
    main()
