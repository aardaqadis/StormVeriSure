"""Complete Stormworks component previews rendered as bounded raster images."""

from dataclasses import dataclass
import math
from pathlib import Path
import queue
import threading
import tkinter as tk

from .vehicle_geometry import _category, load_vehicle_geometry
from .visual_match import match_samples
from .voxel_render import render_voxels


@dataclass(frozen=True)
class VehicleSample:
    points: tuple
    components: int
    bounds: tuple

    @property
    def sampled(self):
        return len(self.points)


def sample_vehicle(path, cancelled=None):
    """Load all positioned components in compact arrays for the preview."""
    return load_vehicle_geometry(path, cancelled=cancelled)


def _project(point, view, yaw, pitch):
    x, y, z = point[:3]
    if view == "Top":
        return x, z, y
    if view == "Side":
        return z, -y, x
    if view == "Front":
        return x, -y, z
    horizontal = math.cos(yaw) * x + math.sin(yaw) * z
    depth = -math.sin(yaw) * x + math.cos(yaw) * z
    vertical = -math.sin(pitch) * depth - math.cos(pitch) * y
    return horizontal, vertical, depth


def _corners(bounds):
    low, high = bounds
    return tuple((x, y, z) for x in (low[0] - .5, high[0] + .5)
                 for y in (low[1] - .5, high[1] + .5)
                 for z in (low[2] - .5, high[2] + .5))


class VehicleVisualizerPanel(tk.Frame):
    """Two vehicle columns with four projections each, rendered only on demand."""

    def __init__(self, parent):
        super().__init__(parent, bg="white")
        self._generation = 0
        self._results = queue.SimpleQueue()
        self._poll_job = None
        self._draw_job = None
        self._loading = False
        self._render_generation = 0
        self._active_render_id = None
        self._render_pending = None
        self._photo_images = {}
        self._frame_keys = {}
        self._samples = {"input": None, "match": None}
        self._overlay = None
        self._canvases = {}
        self._details = {}
        self._detail_text = {}
        self._yaw = 0.68
        self._pitch = 0.52
        self._zoom = 1.0
        self._drag_origin = None
        self._closed = False

        tk.Label(self, bg="white", anchor="w",
                 text="All positioned components loaded; drag a 3D view to rotate, scroll to zoom").pack(
                     fill="x", padx=10, pady=(8, 3))
        columns = tk.Frame(self, bg="white")
        columns.pack(fill="both", expand=True)
        columns.columnconfigure((0, 1), weight=1, uniform="vehicle")
        columns.rowconfigure(0, weight=1)
        for column, (side, title) in enumerate((("input", "Input XML"),
                                                 ("match", "Workshop match"))):
            frame = tk.LabelFrame(columns, text=title, bg="white", fg="#1C1C1C",
                                  padx=6, pady=6)
            frame.grid(row=0, column=column, sticky="nsew", padx=6, pady=4)
            frame.columnconfigure((0, 1, 2), weight=1)
            frame.rowconfigure(1, weight=1)
            detail = tk.Label(frame, bg="white", anchor="w", justify="left",
                              text="Compare a vehicle to load this preview.")
            detail.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 5))
            self._details[side] = detail
            iso = self._make_canvas(frame, side, "3D", 300, 0, 1, 3)
            iso.bind("<ButtonPress-1>", self._start_drag)
            iso.bind("<B1-Motion>", self._drag)
            for column_index, view in enumerate(("Top", "Side", "Front")):
                self._make_canvas(frame, side, view, 145, column_index, 3, 1)
        tk.Label(self, bg="white", anchor="w", fg="#444444",
                 text="Green: matching type and aligned position   Yellow: changed type   "
                      "Red: no corresponding cube   Gray: alignment unverified. "
                      "Cubes are previews, not full block meshes.").pack(
                     fill="x", padx=10, pady=(3, 9))

    def _make_canvas(self, parent, side, view, height, column, row, span):
        wrapper = tk.Frame(parent, bg="white")
        wrapper.grid(row=row, column=column, columnspan=span, sticky="nsew",
                     padx=2, pady=3)
        tk.Label(wrapper, text=view, bg="white", anchor="w").pack(fill="x")
        canvas = tk.Canvas(wrapper, bg="white", height=height,
                           highlightthickness=1, highlightbackground="#BBBBBB")
        canvas.pack(fill="both", expand=True)
        canvas.bind("<Configure>", self._schedule_draw)
        canvas.bind("<MouseWheel>", self._on_wheel)
        self._canvases[(side, view)] = canvas
        return canvas

    def show_files(self, input_path, match_path=None, evidence=()):
        """Load both files and render complete geometry off the Tk event loop."""
        if self._closed:
            return
        self._generation += 1
        generation = self._generation
        self._loading = True
        self._samples = {"input": None, "match": None}
        self._overlay = None
        self._detail_text = {}
        files = {"input": input_path, "match": match_path}
        for side, path in files.items():
            if not path or (side == "match" and not Path(path).is_file()):
                self._details[side].configure(
                    text="Workshop XML is not available locally." if side == "match"
                    else "Choose an input XML file.")
            else:
                self._details[side].configure(text=f"Loading {Path(path).name}...")
        self._draw_all()
        threading.Thread(target=self._load, args=(generation, files, evidence),
                         daemon=True).start()
        if self._poll_job is None:
            self._poll_job = self.after(100, self._poll)

    def _load(self, generation, files, evidence):
        cancelled = lambda: self._closed or generation != self._generation
        try:
            cache = {}
            samples = {}
            for side, path in files.items():
                if cancelled():
                    return
                if not path:
                    continue
                path = Path(path)
                if not path.is_file():
                    continue
                try:
                    key = str(path.resolve())
                    if key not in cache:
                        cache[key] = sample_vehicle(path, cancelled=cancelled)
                    sample, error = cache[key], None
                    samples[side] = sample
                except (OSError, ValueError) as exc:
                    sample, error = None, str(exc)
                if cancelled():
                    return
                self._results.put((generation, side, path.name, sample, error))
            overlay = match_samples(samples.get("input"), samples.get("match"),
                                    evidence, cancelled=cancelled)
            if not cancelled():
                self._results.put((generation, "overlay", None, overlay, None))
        except InterruptedError:
            pass
        except Exception as exc:
            if not cancelled():
                self._results.put((generation, "load_error", None, None, str(exc)))
        finally:
            self._results.put((generation, "done", None, None, None))

    def _poll(self):
        self._poll_job = None
        if self._closed:
            return
        while True:
            try:
                generation, side, name, sample, error = self._results.get_nowait()
            except queue.Empty:
                break
            if side == "render_done":
                if name == self._active_render_id:
                    self._active_render_id = None
                    self._start_render_if_idle()
                continue
            if generation != self._generation:
                continue
            if side == "frame":
                render_id, panel_side, view, frame_key = name
                if render_id == self._render_generation:
                    canvas = self._canvases[(panel_side, view)]
                    photo = tk.PhotoImage(data=sample, format="PPM")
                    canvas.delete("all")
                    canvas.create_image(0, 0, image=photo, anchor="nw")
                    self._photo_images[(panel_side, view)] = photo
                    self._frame_keys[(panel_side, view)] = frame_key
                continue
            if side == "render_error":
                if name == self._render_generation:
                    self._details["input"].configure(text=f"Preview error: {error}")
                continue
            if side == "load_error":
                self._details["input"].configure(text=f"Preview error: {error}")
                continue
            if side == "done":
                self._loading = False
                continue
            if side == "overlay":
                self._overlay = sample
                for label, values in (("input", sample.input_labels),
                                      ("match", sample.match_labels)):
                    if label in self._detail_text:
                        found = values.count("matched")
                        self._details[label].configure(
                            text=self._detail_text[label] +
                            (f"  |  {found:,} matching cubes" if sample.offset
                             else "  |  spatial match unverified"))
                self._schedule_draw()
                continue
            self._samples[side] = sample
            if sample:
                low, high = sample.bounds
                dimensions = " × ".join(str(high[i] - low[i] + 1) for i in range(3))
                self._detail_text[side] = (
                    f"{name}  |  {sample.components:,} components  |  "
                    f"all loaded  |  {dimensions} grid units")
                self._details[side].configure(text=self._detail_text[side])
            else:
                self._details[side].configure(text=f"{name}: {error}")
            self._schedule_draw()
        if self._loading or self._active_render_id is not None or self._render_pending:
            self._poll_job = self.after(100, self._poll)

    def _start_drag(self, event):
        self._drag_origin = (event.x, event.y)

    def _drag(self, event):
        if self._drag_origin is None:
            return
        x, y = self._drag_origin
        self._yaw += (event.x - x) * 0.012
        self._pitch = max(-1.35, min(1.35, self._pitch + (event.y - y) * 0.009))
        self._drag_origin = (event.x, event.y)
        self._schedule_draw()

    def _on_wheel(self, event):
        if event.delta:
            self._zoom = max(.5, min(5.0, self._zoom *
                                     (1.18 if event.delta > 0 else 1 / 1.18)))
            self._schedule_draw()

    def _schedule_draw(self, _event=None):
        if self._closed or self._draw_job is not None:
            return
        self._draw_job = self.after(50, self._draw_all)

    def _draw_all(self):
        self._draw_job = None
        if self._closed:
            return
        self._render_generation += 1
        render_id = self._render_generation
        spans = {}
        for view in ("3D", "Top", "Side", "Front"):
            projections = [
                [_project(corner, view, self._yaw, self._pitch)
                 for corner in _corners(sample.bounds)]
                for sample in self._samples.values() if sample is not None]
            if projections:
                spans[view] = (
                    max(max(point[0] for point in points) -
                        min(point[0] for point in points) for points in projections),
                    max(max(point[1] for point in points) -
                        min(point[1] for point in points) for points in projections))
        tasks = []
        for (side, view), canvas in self._canvases.items():
            labels = (() if self._overlay is None else
                      self._overlay.input_labels if side == "input" else
                      self._overlay.match_labels)
            sample = self._samples[side]
            if sample is None:
                canvas.delete("all")
                self._photo_images.pop((side, view), None)
                self._frame_keys.pop((side, view), None)
                continue
            width = max(canvas.winfo_width(), 2)
            height = max(canvas.winfo_height(), 2)
            frame_key = (id(sample), id(labels), view, width, height,
                         self._zoom, spans.get(view))
            if view == "3D":
                frame_key += (self._yaw, self._pitch)
            if frame_key == self._frame_keys.get((side, view)):
                continue
            tasks.append((side, view, sample, labels,
                          width, height, spans.get(view), frame_key))
        self._render_pending = (
            self._generation, render_id, self._yaw, self._pitch, self._zoom, tasks
        ) if tasks else None
        self._start_render_if_idle()
        if (self._active_render_id is not None or self._render_pending) and self._poll_job is None:
            self._poll_job = self.after(100, self._poll)

    def _start_render_if_idle(self):
        if self._active_render_id is not None or self._render_pending is None:
            return
        request = self._render_pending
        self._render_pending = None
        self._active_render_id = request[1]
        threading.Thread(target=self._render_frames, args=(request,),
                         daemon=True).start()

    def _render_frames(self, request):
        generation, render_id, yaw, pitch, zoom, tasks = request
        cancelled = lambda: (self._closed or generation != self._generation or
                             render_id != self._render_generation)
        try:
            for side, view, sample, labels, width, height, span, frame_key in tasks:
                if cancelled():
                    break
                frame = render_voxels(sample.points, labels, sample.bounds,
                                      view, yaw, pitch, zoom, width, height,
                                      common_span=span, cancelled=cancelled)
                if cancelled():
                    break
                self._results.put((generation, "frame",
                                   (render_id, side, view, frame_key), frame, None))
        except InterruptedError:
            pass
        except Exception as exc:
            self._results.put((generation, "render_error", render_id, None,
                               str(exc)))
        finally:
            self._results.put((generation, "render_done", render_id, None, None))

    def _draw(self, canvas, sample, view, labels=(), common_span=None):
        """Draw one frame directly; the live panel uses the background worker."""
        canvas.delete("all")
        if sample is None:
            return
        frame = render_voxels(sample.points, labels, sample.bounds, view,
                              self._yaw, self._pitch, self._zoom,
                              max(canvas.winfo_width(), 2),
                              max(canvas.winfo_height(), 2), common_span)
        photo = tk.PhotoImage(data=frame, format="PPM")
        canvas.create_image(0, 0, image=photo, anchor="nw")
        canvas._vehicle_image = photo

    def close(self):
        """Stop polling when the containing app is closed."""
        self._closed = True
        self._generation += 1
        self._render_generation += 1
        self._render_pending = None
        self._photo_images.clear()
        self._frame_keys.clear()
        for job in (self._poll_job, self._draw_job):
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        self._poll_job = self._draw_job = None
