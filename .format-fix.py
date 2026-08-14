from pathlib import Path

path = Path("furl_ctx/cache/compression_store.py")
text = path.read_text()
old = '''            child = self._delete_cascade_from_graph(\n                nested_hash, graph=graph, visited=visited\n            )'''
new = '''            child = self._delete_cascade_from_graph(nested_hash, graph=graph, visited=visited)'''
if text.count(old) != 1:
    raise SystemExit(f"expected one formatter target, found {text.count(old)}")
path.write_text(text.replace(old, new, 1))
