"""3D model converters: STL, OBJ, PLY and GLB to each other.

Uses trimesh, which needs only numpy (already installed with pdf2docx).

* STL and PLY hold one mesh, so a model with several parts (OBJ groups, GLB
  nodes) is merged into one mesh with each part's position applied.
* OBJ and GLB keep the separate parts. Colours and materials are kept where
  the target format has them (STL has none). An OBJ with materials or
  textures comes back as a zip with its .mtl and texture images.
* Files are read from memory with no file resolver, so an OBJ's "mtllib" or a
  GLB's external buffers can never read other files on the server. An OBJ
  uploaded without its .mtl still converts, without materials.

Models come from strangers, so element counts that a PLY header promises and
the number of faces after loading are capped (MAX_MODEL_FACES).
"""
import io
import os
import re
from pathlib import Path

from errors import ConversionError

MODEL_IN = ["stl", "obj", "ply", "glb"]
MODEL_OUT = ["stl", "obj", "ply", "glb"]

MAX_FACES = int(os.environ.get("MAX_MODEL_FACES", "10000000"))


def targets_for(ext: str) -> list[str]:
    return [t for t in MODEL_OUT if t != ext]


# ---------- Loading ----------
_PLY_ELEMENT = re.compile(rb"^element\s+(\w+)\s+(\d+)", re.MULTILINE)


def _check_ply_header(src: Path, data: bytes) -> None:
    end = data.find(b"end_header")
    if not data.startswith(b"ply") or end < 0:
        raise ConversionError(f"{src.name} doesn't look like a real .ply file. It may be corrupted or renamed.")
    for _, count in _PLY_ELEMENT.findall(data[:end]):
        if int(count) > MAX_FACES * 3:
            raise ConversionError(f"{src.name} is too large: it has more than {MAX_FACES:,} faces.")


def _load(src: Path, ext: str):
    """Returns a trimesh.Scene with at least one mesh that has faces."""
    import trimesh

    data = src.read_bytes()
    if ext == "ply":
        _check_ply_header(src, data)
    try:
        # BytesIO has no file name, so trimesh gets no resolver and can't open any other file.
        loaded = trimesh.load(io.BytesIO(data), file_type=ext, resolver=None, process=True)
    except Exception as exc:
        msg = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise ConversionError(f"Can't read {src.name} as a 3D model ({msg}).")

    scene = loaded if isinstance(loaded, trimesh.Scene) else trimesh.Scene(loaded)
    meshes = [g for g in scene.geometry.values() if isinstance(g, trimesh.Trimesh) and len(g.faces)]
    if not meshes:
        raise ConversionError(f"{src.name} has no surfaces to convert (it may be empty, a point cloud or only lines).")
    faces = sum(len(m.faces) for m in meshes)
    if faces > MAX_FACES:
        raise ConversionError(f"{src.name} is too large: it has {faces:,} faces and the limit is {MAX_FACES:,}.")
    # Drop point clouds and lines: none of the targets can hold them next to meshes reliably.
    for name, geom in list(scene.geometry.items()):
        if not (isinstance(geom, trimesh.Trimesh) and len(geom.faces)):
            scene.delete_geometry(name)
    return scene


def _colors(geom, kind: str):
    visual = getattr(geom, "visual", None)
    if getattr(visual, "kind", None) != kind:
        return None
    return visual.face_colors if kind == "face" else visual.vertex_colors


def _single_mesh(scene):
    """Join every part into one mesh, with each part's position applied.

    Done with numpy rather than scene.to_mesh(): trimesh copies face colours via
    scipy, which the image doesn't ship. Colours survive when every part has the
    same kind (all per-face or all per-vertex); textures can't go into STL/PLY."""
    import numpy as np
    import trimesh

    parts = []
    for node in scene.graph.nodes_geometry:
        transform, name = scene.graph[node]
        geom = scene.geometry.get(name)
        if isinstance(geom, trimesh.Trimesh) and len(geom.faces):
            parts.append((geom, transform))
    if not parts:
        raise ConversionError("The model has no surfaces to convert.")

    vertices, faces, offset = [], [], 0
    for geom, transform in parts:
        vertices.append(trimesh.transformations.transform_points(geom.vertices, transform))
        faces.append(geom.faces + offset)
        offset += len(geom.vertices)
    kwargs = {}
    for kind, key in (("face", "face_colors"), ("vertex", "vertex_colors")):
        cols = [_colors(g, kind) for g, _ in parts]
        if all(c is not None for c in cols):
            kwargs[key] = np.vstack(cols)
            break
    return trimesh.Trimesh(vertices=np.vstack(vertices), faces=np.vstack(faces), process=False, **kwargs)


# ---------- Writing ----------
def _face_colors_to_vertex(scene) -> None:
    """GLB and OBJ store colours per vertex. trimesh's own face-to-vertex step needs
    scipy (about 100 MB), so average each vertex's face colours with numpy instead."""
    import numpy as np
    from trimesh.visual import ColorVisuals

    for geom in scene.geometry.values():
        visual = getattr(geom, "visual", None)
        if getattr(visual, "kind", None) != "face":
            continue
        face_rgba = np.asarray(visual.face_colors, dtype=np.float64)
        sums = np.zeros((len(geom.vertices), 4))
        counts = np.zeros(len(geom.vertices))
        for corner in range(geom.faces.shape[1]):
            np.add.at(sums, geom.faces[:, corner], face_rgba)
            np.add.at(counts, geom.faces[:, corner], 1)
        rgba = (sums / np.maximum(counts, 1)[:, None]).round().clip(0, 255).astype(np.uint8)
        geom.visual = ColorVisuals(geom, vertex_colors=rgba)


def _write_obj(scene, out: Path) -> list[Path]:
    from trimesh.exchange.obj import export_obj

    mtl_name = f"{out.stem}.mtl"
    text, extras = export_obj(scene, include_texture=True, return_texture=True, mtl_name=mtl_name)
    out.write_text(text, encoding="utf-8")
    outs = [out]
    for name, blob in (extras or {}).items():
        safe = Path(str(name)).name  # never write outside the output folder
        if not safe:
            continue
        path = out.with_name(safe)
        path.write_bytes(blob)
        outs.append(path)
    return outs


def convert_model(src: Path, target: str, out_dir: Path) -> list[Path]:
    ext = src.suffix.lower().lstrip(".")
    scene = _load(src, ext)
    out = out_dir / f"{src.stem}.{target}"
    try:
        if target == "stl":
            out.write_bytes(_single_mesh(scene).export(file_type="stl"))
        elif target == "ply":
            out.write_bytes(_single_mesh(scene).export(file_type="ply"))
        elif target == "glb":
            _face_colors_to_vertex(scene)
            out.write_bytes(scene.export(file_type="glb"))
        elif target == "obj":
            _face_colors_to_vertex(scene)
            return _write_obj(scene, out)
        else:
            raise ConversionError(f"Can't convert {ext.upper()} to {target.upper()}.")
        return [out]
    except ConversionError:
        raise
    except Exception as exc:
        raise ConversionError(f"Couldn't convert {src.name} to {target.upper()} ({exc}).")
