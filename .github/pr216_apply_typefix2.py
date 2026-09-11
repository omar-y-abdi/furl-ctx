from pathlib import Path

path = Path("furl_ctx/cache/compression_store.py")
text = path.read_text()

old_identity = '            str(coordinated.coordination_identity)\n'
new_identity = '            str(getattr(coordinated, "coordination_identity", None))\n'
if text.count(old_identity) != 1:
    raise SystemExit("coordination identity target changed")
text = text.replace(old_identity, new_identity, 1)

old_record = '''        try:\n            record = getter(hash_key)\n        except StorageUnavailableError:\n            raise\n        except Exception as exc:\n            raise StorageUnavailableError(f"binding read failed for {hash_key}") from exc\n        return record\n'''
new_record = '''        try:\n            record = getter(hash_key)\n        except StorageUnavailableError:\n            raise\n        except Exception as exc:\n            raise StorageUnavailableError(f"binding read failed for {hash_key}") from exc\n        if record is None:\n            return None\n        fingerprint, conflicted = record\n        return str(fingerprint), bool(conflicted)\n'''
if text.count(old_record) != 1:
    raise SystemExit("binding record target changed")
text = text.replace(old_record, new_record, 1)

path.write_text(text)
