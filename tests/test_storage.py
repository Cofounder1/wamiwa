"""Uploads in a private Supabase Storage bucket, against a fake that answers like its REST API."""
import json

import httpx

from app.infra.storage import SupabaseFileStorage, _cached

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class FakeSupabase:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.headers_seen: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.headers_seen.append(dict(request.headers))
        path = request.url.path.removeprefix("/storage/v1")
        if request.method == "POST" and path.startswith("/object/wamiwa-uploads/"):
            key = path.removeprefix("/object/wamiwa-uploads/")
            self.objects[key] = (request.content, request.headers["content-type"])
            return httpx.Response(200, json={"Key": f"wamiwa-uploads/{key}"})
        if request.method == "GET" and path.startswith("/object/wamiwa-uploads/"):
            found = self.objects.get(path.removeprefix("/object/wamiwa-uploads/"))
            if not found:
                return httpx.Response(404, json={"error": "not_found"})
            if rng := request.headers.get("range"):  # like Supabase: a byte range comes back as 206
                first, last = (int(x) for x in rng.removeprefix("bytes=").split("-"))
                part = found[0][first:last + 1]
                return httpx.Response(206, content=part, headers={
                    "Content-Range": f"bytes {first}-{first + len(part) - 1}/{len(found[0])}"})
            return httpx.Response(200, content=found[0])
        if request.method == "DELETE" and path == "/object/wamiwa-uploads":
            for key in json.loads(request.content)["prefixes"]:
                self.objects.pop(key, None)
            return httpx.Response(200, json=[])
        return httpx.Response(404)


async def test_uploads_go_to_the_bucket_and_are_served_and_deleted_from_it(api, tmp_path, monkeypatch):
    fake = FakeSupabase()
    store = SupabaseFileStorage(str(tmp_path), "https://proj.supabase.co", "sb_secret_test", "wamiwa-uploads",
                                transport=httpx.MockTransport(fake.handler))
    s = api.infra.settings
    s.upload_dir, s.supabase_url, s.supabase_secret_key = str(tmp_path), "https://proj.supabase.co", "sb_secret_test"
    _cached.cache_clear()
    monkeypatch.setattr("app.infra.storage._cached", lambda *args: store)

    user = await api.signup("ada")
    up = await api.ok("POST", "/uploads", user, data={"kind": "image"}, files={"file": ("p.png", PNG, "image/png")})
    assert up["ref"] in fake.objects and fake.objects[up["ref"]][1] == "image/png"
    assert not any(tmp_path.rglob("*.png"))  # nothing left on the server's disk
    assert fake.headers_seen[0]["apikey"] == "sb_secret_test" and "authorization" not in fake.headers_seen[0]

    served = await api.call("GET", up["url"], expect=200)
    assert served.content == fake.objects[up["ref"]][0] and served.headers["content-type"] == "image/png"
    assert served.headers["x-content-type-options"] == "nosniff"
    await api.call("GET", "/media/image/" + "f" * 32 + ".png", expect=404)
    # Phones and Safari ask for videos in byte ranges; the bucket's partial answer is passed on.
    part = await api.call("GET", up["url"], expect=206, headers={"Range": "bytes=0-7"})
    assert part.content == PNG[:8] and part.headers["content-range"] == f"bytes 0-7/{len(PNG)}"

    await store.delete(up["ref"])
    assert up["ref"] not in fake.objects


async def test_the_bucket_caps_uploads_at_its_file_limit(api):
    from app.api.routers.uploads import upload_limit

    s = api.infra.settings
    assert upload_limit(s, "video") == 200 * 1024 * 1024  # local disk: the kind's own limit
    s.supabase_url, s.supabase_secret_key = "https://proj.supabase.co", "sb_secret_x"
    assert upload_limit(s, "video") == 50 * 1024 * 1024  # Supabase's free plan: 50 MB a file
    assert upload_limit(s, "image") == 10 * 1024 * 1024


async def test_files_on_disk_are_served_in_byte_ranges(tmp_path):
    from app.infra.storage import LocalFileStorage

    store = LocalFileStorage(str(tmp_path))
    key = "video/" + "a" * 32 + ".mp4"
    data = bytes(range(256)) * 10

    async def chunks():
        yield data
    await store.save(key, chunks(), 10_000)

    async def body(served):
        return b"".join([c async for c in served.chunks])

    whole = await store.serve(key)
    assert whole.status == 200 and await body(whole) == data and whole.headers["Accept-Ranges"] == "bytes"
    part = await store.serve(key, "bytes=10-19")
    assert part.status == 206 and await body(part) == data[10:20]
    assert part.headers["Content-Range"] == f"bytes 10-19/{len(data)}" and part.headers["Content-Length"] == "10"
    tail = await store.serve(key, "bytes=-5")
    assert await body(tail) == data[-5:]
    assert (await store.serve(key, "bytes=99999-")).status == 416
    assert await store.serve("video/" + "b" * 32 + ".mp4") is None
