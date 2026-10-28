//! Кэш потока и офлайн-треки (Windows). Контракт — docs/design/APPS_API.md, «Кэш и офлайн (Windows)».
//!
//! Протокол `pimusic://` (в WebView2 — `http://pimusic.localhost/`):
//!   GET /stream/<id>?src=<подписанный URL потока на сервере> — отдаёт трек с диска, если он есть целиком
//!   (офлайн или кэш потока), иначе запускает полную загрузку в кэш и отдаёт уже скачанное (Range, 206);
//!   далёкая перемотка вперёд недокачанного — диапазоном прямо с сервера.
//! Кэш потока — LRU по времени последнего доступа с лимитом (по умолчанию 1 ГиБ); офлайн-треки лежат до удаления.
//! Каталоги — по origin сервера: %LOCALAPPDATA%\io.github.ikriu.pimusic\media\<origin>\{stream,offline}.

use std::collections::HashMap;
use std::fs;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};
use tauri::http::{header, Request, Response, StatusCode};
use tauri::{AppHandle, Manager};
use url::Url;

pub const SCHEME: &str = "pimusic";
const DEFAULT_LIMIT: u64 = 1 << 30;
const MIN_LIMIT: u64 = 256 << 20;
/// наибольший кусок одного ответа протоколу
const CHUNK: u64 = 4 << 20;
/// насколько вперёд от скачанного ждать загрузку, а не ходить на сервер диапазоном
const AHEAD: u64 = 3 << 20;
/// сколько ждать первых байтов загрузки, прежде чем отдать диапазон прямо с сервера
const WAIT: Duration = Duration::from_secs(12);
/// недавно отданные файлы (текущий/следующий трек) не вытесняются
const PROTECT: Duration = Duration::from_secs(15 * 60);

#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
pub enum Kind {
    Stream,
    Offline,
}

#[derive(Default)]
struct Job {
    downloaded: AtomicU64,
    /// 0 — размер ещё неизвестен
    total: AtomicU64,
    started: AtomicBool,
    failed: AtomicBool,
    cancelled: AtomicBool,
    content_type: Mutex<String>,
}

pub struct Cache {
    stream_dir: PathBuf,
    offline_dir: PathBuf,
    settings_file: PathBuf,
    jobs: Mutex<HashMap<(Kind, String), Arc<Job>>>,
    client: reqwest::Client,
    limit: AtomicU64,
}

fn now_unix() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

/// id трека из API: цифры/буквы, _ и -, не длиннее 64 (идёт в имя файла)
fn valid_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 64 && id.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

impl Cache {
    pub fn new(app: &AppHandle, server_origin: Option<&str>) -> Self {
        let base = app.path().app_local_data_dir().unwrap_or_else(|_| std::env::temp_dir().join("pimusic"));
        let ns: String = server_origin
            .unwrap_or("none")
            .chars()
            .map(|c| if c.is_ascii_alphanumeric() { c } else { '_' })
            .collect();
        let root = base.join("media").join(ns);
        let stream_dir = root.join("stream");
        let offline_dir = root.join("offline");
        let _ = fs::create_dir_all(&stream_dir);
        let _ = fs::create_dir_all(&offline_dir);
        let settings_file = base.join("media").join("cache.json");
        let limit = fs::read_to_string(&settings_file)
            .ok()
            .and_then(|s| serde_json::from_str::<Value>(&s).ok())
            .and_then(|v| v.get("streamLimit").and_then(Value::as_u64))
            .unwrap_or(DEFAULT_LIMIT)
            .max(MIN_LIMIT);
        let client = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(15))
            .read_timeout(Duration::from_secs(60))
            .user_agent(format!("PiMusic-Windows/{}", app.package_info().version))
            .build()
            .unwrap_or_default();
        // недокачанное с прошлого запуска
        for d in [&stream_dir, &offline_dir] {
            if let Ok(rd) = fs::read_dir(d) {
                for e in rd.flatten() {
                    if e.path().extension().and_then(|x| x.to_str()) == Some("part") {
                        let _ = fs::remove_file(e.path());
                    }
                }
            }
        }
        Self {
            stream_dir,
            offline_dir,
            settings_file,
            jobs: Mutex::new(HashMap::new()),
            client,
            limit: AtomicU64::new(limit),
        }
    }

    fn dir(&self, kind: Kind) -> &Path {
        match kind {
            Kind::Stream => &self.stream_dir,
            Kind::Offline => &self.offline_dir,
        }
    }
    fn bin(&self, kind: Kind, id: &str) -> PathBuf {
        self.dir(kind).join(format!("{id}.bin"))
    }
    fn part(&self, kind: Kind, id: &str) -> PathBuf {
        self.dir(kind).join(format!("{id}.part"))
    }
    fn meta_path(&self, kind: Kind, id: &str) -> PathBuf {
        self.dir(kind).join(format!("{id}.json"))
    }
    fn read_meta(&self, kind: Kind, id: &str) -> Value {
        fs::read_to_string(self.meta_path(kind, id))
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or(Value::Null)
    }

    /// Целый файл трека: сначала офлайн, затем кэш потока (доступ обновляет время для LRU).
    fn complete(&self, id: &str) -> Option<(PathBuf, u64, String)> {
        for kind in [Kind::Offline, Kind::Stream] {
            let p = self.bin(kind, id);
            if let Ok(md) = fs::metadata(&p) {
                if kind == Kind::Stream {
                    if let Ok(f) = fs::File::options().write(true).open(&p) {
                        let _ = f.set_modified(SystemTime::now());
                    }
                }
                let ct = self.read_meta(kind, id)
                    .get("contentType")
                    .and_then(Value::as_str)
                    .unwrap_or("application/octet-stream")
                    .to_string();
                return Some((p, md.len(), ct));
            }
        }
        None
    }

    fn job(&self, kind: Kind, id: &str) -> Option<Arc<Job>> {
        self.jobs.lock().unwrap().get(&(kind, id.to_string())).cloned()
    }

    fn save_limit(&self) {
        let v = json!({ "streamLimit": self.limit.load(Ordering::SeqCst) });
        let _ = fs::write(&self.settings_file, v.to_string());
    }

    /// LRU: самые давние файлы кэша потока, пока сумма больше лимита (кроме качающихся и недавно отданных).
    fn evict(&self) {
        let limit = self.limit.load(Ordering::SeqCst);
        let busy: Vec<String> = self.jobs.lock().unwrap().keys().filter(|k| k.0 == Kind::Stream).map(|k| k.1.clone()).collect();
        let mut files: Vec<(SystemTime, u64, String)> = Vec::new();
        let mut sum = 0u64;
        if let Ok(rd) = fs::read_dir(&self.stream_dir) {
            for e in rd.flatten() {
                let p = e.path();
                if p.extension().and_then(|x| x.to_str()) != Some("bin") {
                    continue;
                }
                let Some(id) = p.file_stem().and_then(|s| s.to_str()).map(str::to_string) else { continue };
                if let Ok(md) = e.metadata() {
                    sum += md.len();
                    files.push((md.modified().unwrap_or(UNIX_EPOCH), md.len(), id));
                }
            }
        }
        files.sort();
        let now = SystemTime::now();
        for (mtime, size, id) in files {
            if sum <= limit {
                break;
            }
            if busy.contains(&id) || now.duration_since(mtime).map(|d| d < PROTECT).unwrap_or(true) {
                continue;
            }
            let _ = fs::remove_file(self.bin(Kind::Stream, &id));
            let _ = fs::remove_file(self.meta_path(Kind::Stream, &id));
            sum = sum.saturating_sub(size);
        }
    }

    fn stats(&self, kind: Kind) -> (u64, u64) {
        let (mut n, mut bytes) = (0u64, 0u64);
        if let Ok(rd) = fs::read_dir(self.dir(kind)) {
            for e in rd.flatten() {
                if e.path().extension().and_then(|x| x.to_str()) == Some("bin") {
                    n += 1;
                    bytes += e.metadata().map(|m| m.len()).unwrap_or(0);
                }
            }
        }
        (n, bytes)
    }
}

/// Адрес потока должен вести на настроенный сервер (иначе протокол был бы прокси куда угодно).
fn check_src(app: &AppHandle, src: &str) -> Result<Url, String> {
    let u = Url::parse(src).map_err(|_| "неверный адрес потока".to_string())?;
    let origin = crate::origin_of(&u);
    if origin.is_none() || origin != crate::server_origin(app) {
        return Err("адрес потока не на сервере PiMusic".into());
    }
    Ok(u)
}

/// Запустить загрузку целиком (если ещё не идёт и файла нет). Возвращает задачу или None — файл уже есть.
fn ensure(app: &AppHandle, kind: Kind, id: &str, src: &str, meta: Value) -> Result<Option<Arc<Job>>, String> {
    if !valid_id(id) {
        return Err("неверный trackId".into());
    }
    let c = app.state::<Cache>();
    if c.bin(kind, id).exists() {
        return Ok(None);
    }
    let url = check_src(app, src)?;
    let mut jobs = c.jobs.lock().unwrap();
    if let Some(j) = jobs.get(&(kind, id.to_string())) {
        if !j.failed.load(Ordering::SeqCst) {
            return Ok(Some(j.clone()));
        }
    }
    let job = Arc::new(Job::default());
    jobs.insert((kind, id.to_string()), job.clone());
    drop(jobs);
    let (app2, id2, job2) = (app.clone(), id.to_string(), job.clone());
    tauri::async_runtime::spawn(async move { run(app2, kind, id2, url, meta, job2).await });
    Ok(Some(job))
}

async fn run(app: AppHandle, kind: Kind, id: String, url: Url, meta: Value, job: Arc<Job>) {
    let res = download(&app, kind, &id, &url, &meta, &job).await;
    let c = app.state::<Cache>();
    if res.is_err() {
        job.failed.store(true, Ordering::SeqCst);
        let _ = fs::remove_file(c.part(kind, &id));
    }
    c.jobs.lock().unwrap().remove(&(kind, id.clone()));
    match (kind, res) {
        (Kind::Stream, Ok(_)) => c.evict(),
        (Kind::Stream, Err(e)) => eprintln!("cache stream {id}: {e}"),
        (Kind::Offline, Ok(size)) => crate::emit_js(&app, "offlineDone", json!({ "trackId": id, "size": size })),
        (Kind::Offline, Err(e)) => {
            eprintln!("cache offline {id}: {e}");
            crate::emit_js(&app, "offlineError", json!({ "trackId": id, "message": e }));
        }
    }
}

fn write_meta(c: &Cache, kind: Kind, id: &str, size: u64, ct: &str, meta: &Value) -> Result<(), String> {
    let m = json!({ "trackId": id, "size": size, "contentType": ct, "addedAt": now_unix(), "meta": meta });
    fs::write(c.meta_path(kind, id), m.to_string()).map_err(|e| e.to_string())
}

async fn download(app: &AppHandle, kind: Kind, id: &str, url: &Url, meta: &Value, job: &Job) -> Result<u64, String> {
    let c = app.state::<Cache>();
    // офлайн: трек уже целиком в кэше потока — просто копия
    if kind == Kind::Offline {
        let from = c.bin(Kind::Stream, id);
        if from.exists() {
            let ct = c.read_meta(Kind::Stream, id).get("contentType").and_then(Value::as_str).unwrap_or("application/octet-stream").to_string();
            let tmp = c.part(kind, id);
            let size = fs::copy(&from, &tmp).map_err(|e| e.to_string())?;
            fs::rename(&tmp, c.bin(kind, id)).map_err(|e| e.to_string())?;
            write_meta(&c, kind, id, size, &ct, meta)?;
            return Ok(size);
        }
    }
    let mut resp = c.client.get(url.clone()).send().await.map_err(|e| format!("нет связи с сервером: {e}"))?;
    if resp.status() != reqwest::StatusCode::OK {
        return Err(format!("сервер ответил {}", resp.status().as_u16()));
    }
    let total = resp.content_length().unwrap_or(0);
    let ct = resp
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("application/octet-stream")
        .to_string();
    *job.content_type.lock().unwrap() = ct.clone();
    job.total.store(total, Ordering::SeqCst);
    let part = c.part(kind, id);
    let mut f = fs::File::create(&part).map_err(|e| e.to_string())?;
    job.started.store(true, Ordering::SeqCst);
    let (mut got, mut last_emit) = (0u64, 0u64);
    while let Some(chunk) = resp.chunk().await.map_err(|e| format!("обрыв загрузки: {e}"))? {
        if job.cancelled.load(Ordering::SeqCst) {
            return Err("отменено".into());
        }
        f.write_all(&chunk).map_err(|e| e.to_string())?;
        got += chunk.len() as u64;
        job.downloaded.store(got, Ordering::SeqCst);
        if kind == Kind::Offline && (got - last_emit >= 1 << 20 || got == total) {
            last_emit = got;
            crate::emit_js(app, "offlineProgress", json!({ "trackId": id, "downloaded": got, "total": if total > 0 { Some(total) } else { None } }));
        }
    }
    f.flush().map_err(|e| e.to_string())?;
    drop(f);
    if total > 0 && got != total {
        return Err(format!("загрузка неполная: {got} из {total}"));
    }
    if total == 0 {
        job.total.store(got, Ordering::SeqCst);
    }
    let bin = c.bin(kind, id);
    let _ = fs::remove_file(&bin);
    fs::rename(&part, &bin).map_err(|e| e.to_string())?;
    write_meta(&c, kind, id, got, &ct, meta)?;
    Ok(got)
}

// ------------------------------------------------------------------------------------------- протокол

fn reply(status: StatusCode, ct: &str, body: Vec<u8>) -> Response<Vec<u8>> {
    Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, ct)
        .header(header::ACCESS_CONTROL_ALLOW_ORIGIN, "*")
        .header(header::CACHE_CONTROL, "no-store")
        .body(body)
        .unwrap_or_default()
}

fn err(status: StatusCode, msg: &str) -> Response<Vec<u8>> {
    reply(status, "text/plain; charset=utf-8", msg.as_bytes().to_vec())
}

/// "bytes=a-b" / "bytes=a-" → (a, Some(b) | None); отсутствует или непонятен — (0, None)
fn parse_range(h: Option<&str>) -> (u64, Option<u64>, bool) {
    let Some(r) = h.and_then(|v| v.trim().strip_prefix("bytes=")) else { return (0, None, false) };
    let r = r.split(',').next().unwrap_or("");
    let (a, b) = r.split_once('-').unwrap_or((r, ""));
    match (a.trim().parse::<u64>(), b.trim()) {
        (Ok(a), "") => (a, None, true),
        (Ok(a), b) => (a, b.parse::<u64>().ok(), true),
        _ => (0, None, false),
    }
}

/// Кусок файла [a, end] (end включительно) при известном размере total.
fn serve_file(path: &Path, ct: &str, a: u64, b: Option<u64>, ranged: bool, avail: u64, total: u64) -> Response<Vec<u8>> {
    if total > 0 && a >= total {
        return Response::builder()
            .status(StatusCode::RANGE_NOT_SATISFIABLE)
            .header(header::CONTENT_RANGE, format!("bytes */{total}"))
            .header(header::ACCESS_CONTROL_ALLOW_ORIGIN, "*")
            .body(Vec::new())
            .unwrap_or_default();
    }
    let whole = !ranged && avail == total && total <= CHUNK * 4;
    let mut end = avail.saturating_sub(1).min(a + CHUNK - 1);
    if let Some(b) = b {
        end = end.min(b);
    }
    if whole {
        end = total.saturating_sub(1);
    }
    let len = end + 1 - a;
    let mut buf = vec![0u8; len as usize];
    let read = fs::File::open(path).and_then(|mut f| {
        f.seek(SeekFrom::Start(a))?;
        f.read_exact(&mut buf)
    });
    if let Err(e) = read {
        return err(StatusCode::INTERNAL_SERVER_ERROR, &e.to_string());
    }
    let mut r = Response::builder()
        .header(header::CONTENT_TYPE, ct)
        .header(header::ACCEPT_RANGES, "bytes")
        .header(header::CONTENT_LENGTH, len.to_string())
        .header(header::ACCESS_CONTROL_ALLOW_ORIGIN, "*")
        .header(header::CACHE_CONTROL, "no-store");
    r = if whole {
        r.status(StatusCode::OK)
    } else {
        r.status(StatusCode::PARTIAL_CONTENT).header(header::CONTENT_RANGE, format!("bytes {a}-{end}/{total}"))
    };
    r.body(buf).unwrap_or_default()
}

/// Диапазон прямо с сервера (перемотка далеко вперёд или загрузка в кэш не удалась).
async fn upstream(app: &AppHandle, src: &str, a: u64, b: Option<u64>) -> Response<Vec<u8>> {
    let url = match check_src(app, src) {
        Ok(u) => u,
        Err(e) => return err(StatusCode::FORBIDDEN, &e),
    };
    let end = b.unwrap_or(a + CHUNK - 1).min(a + CHUNK - 1);
    let c = app.state::<Cache>();
    let resp = match c.client.get(url).header(reqwest::header::RANGE, format!("bytes={a}-{end}")).send().await {
        Ok(r) => r,
        Err(e) => return err(StatusCode::BAD_GATEWAY, &format!("нет связи с сервером: {e}")),
    };
    let status = StatusCode::from_u16(resp.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
    let pass = |n: reqwest::header::HeaderName| resp.headers().get(&n).and_then(|v| v.to_str().ok()).map(str::to_string);
    let ct = pass(reqwest::header::CONTENT_TYPE).unwrap_or_else(|| "application/octet-stream".into());
    let cr = pass(reqwest::header::CONTENT_RANGE);
    let body = match resp.bytes().await {
        Ok(b) => b.to_vec(),
        Err(e) => return err(StatusCode::BAD_GATEWAY, &format!("обрыв: {e}")),
    };
    let mut r = Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, ct)
        .header(header::ACCEPT_RANGES, "bytes")
        .header(header::CONTENT_LENGTH, body.len().to_string())
        .header(header::ACCESS_CONTROL_ALLOW_ORIGIN, "*")
        .header(header::CACHE_CONTROL, "no-store");
    if let Some(cr) = cr {
        r = r.header(header::CONTENT_RANGE, cr);
    }
    r.body(body).unwrap_or_default()
}

pub async fn handle(app: AppHandle, req: Request<Vec<u8>>) -> Response<Vec<u8>> {
    if req.method() == tauri::http::Method::OPTIONS {
        return Response::builder()
            .status(StatusCode::NO_CONTENT)
            .header(header::ACCESS_CONTROL_ALLOW_ORIGIN, "*")
            .header(header::ACCESS_CONTROL_ALLOW_HEADERS, "Range")
            .body(Vec::new())
            .unwrap_or_default();
    }
    let uri = req.uri();
    let segs: Vec<&str> = uri.path().trim_matches('/').split('/').collect();
    let id = match segs.as_slice() {
        ["stream", id] | ["offline", id] => (*id).to_string(),
        _ => return err(StatusCode::NOT_FOUND, "нет такого пути"),
    };
    if !valid_id(&id) {
        return err(StatusCode::BAD_REQUEST, "неверный trackId");
    }
    let src: Option<String> = uri.query().and_then(|q| {
        url::form_urlencoded::parse(q.as_bytes()).find(|(k, _)| k == "src").map(|(_, v)| v.into_owned())
    });
    let (a, b, ranged) = parse_range(req.headers().get(header::RANGE).and_then(|v| v.to_str().ok()));
    let c = app.state::<Cache>();
    let deadline = Instant::now() + WAIT;
    loop {
        if let Some((path, size, ct)) = c.complete(&id) {
            return serve_file(&path, &ct, a, b, ranged, size, size);
        }
        let Some(src) = src.as_deref() else { return err(StatusCode::NOT_FOUND, "трека нет на диске") };
        let job = match c.job(Kind::Stream, &id) {
            Some(j) => Some(j),
            None => match ensure(&app, Kind::Stream, &id, src, Value::Null) {
                Ok(j) => j,
                Err(e) => return err(StatusCode::FORBIDDEN, &e),
            },
        };
        let Some(job) = job else { continue }; // файл только что докачался
        if job.failed.load(Ordering::SeqCst) {
            return upstream(&app, src, a, b).await;
        }
        if job.started.load(Ordering::SeqCst) {
            let (have, total) = (job.downloaded.load(Ordering::SeqCst), job.total.load(Ordering::SeqCst));
            if total > 0 && have > a {
                let ct = job.content_type.lock().unwrap().clone();
                return serve_file(&c.part(Kind::Stream, &id), &ct, a, b, true, have, total);
            }
            if total > 0 && a >= total {
                return serve_file(&c.part(Kind::Stream, &id), "application/octet-stream", a, b, true, have, total);
            }
            if a > have + AHEAD {
                return upstream(&app, src, a, b).await;
            }
        }
        if Instant::now() > deadline {
            return upstream(&app, src, a, b).await;
        }
        tokio::time::sleep(Duration::from_millis(40)).await;
    }
}

// ------------------------------------------------------------------------------------------- мост

fn list_offline(c: &Cache) -> Vec<Value> {
    let mut out = Vec::new();
    if let Ok(rd) = fs::read_dir(&c.offline_dir) {
        for e in rd.flatten() {
            let p = e.path();
            if p.extension().and_then(|x| x.to_str()) != Some("bin") {
                continue;
            }
            let Some(id) = p.file_stem().and_then(|s| s.to_str()) else { continue };
            let m = c.read_meta(Kind::Offline, id);
            out.push(json!({
                "trackId": id,
                "size": e.metadata().map(|m| m.len()).unwrap_or(0),
                "contentType": m.get("contentType").cloned().unwrap_or(Value::Null),
                "addedAt": m.get("addedAt").cloned().unwrap_or(Value::Null),
                "meta": m.get("meta").cloned().unwrap_or(Value::Null),
            }));
        }
    }
    out.sort_by_key(|v| std::cmp::Reverse(v.get("addedAt").and_then(Value::as_u64).unwrap_or(0)));
    out
}

fn arg_id(args: &Value) -> Result<String, String> {
    let id = match args.get("trackId") {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        _ => return Err("нужен trackId".into()),
    };
    if valid_id(&id) { Ok(id) } else { Err("неверный trackId".into()) }
}

fn arg_url(args: &Value) -> Result<String, String> {
    args.get("url").and_then(Value::as_str).map(str::to_string).ok_or_else(|| "нужен url".into())
}

fn remove_kind(c: &Cache, kind: Kind, id: &str) {
    if let Some(j) = c.job(kind, id) {
        j.cancelled.store(true, Ordering::SeqCst);
    }
    let _ = fs::remove_file(c.bin(kind, id));
    let _ = fs::remove_file(c.meta_path(kind, id));
}

/// Методы моста кэша; None — метод не про кэш.
pub fn command(app: &AppHandle, method: &str, args: &Value) -> Option<Result<Value, String>> {
    let c = app.state::<Cache>();
    let res = match method {
        "cachePrefetch" => (|| -> Result<Value, String> {
            let id = arg_id(args)?;
            let job = ensure(app, Kind::Stream, &id, &arg_url(args)?, Value::Null)?;
            Ok(json!({ "trackId": id, "cached": job.is_none() }))
        })(),
        "cacheStatus" => (|| -> Result<Value, String> {
            let id = arg_id(args)?;
            let offline = c.bin(Kind::Offline, &id).exists();
            let cached = offline || c.bin(Kind::Stream, &id).exists();
            let job = c.job(Kind::Offline, &id).or_else(|| c.job(Kind::Stream, &id));
            Ok(json!({
                "trackId": id, "offline": offline, "cached": cached,
                "downloading": job.as_ref().map(|j| !j.failed.load(Ordering::SeqCst)).unwrap_or(false),
                "downloaded": job.as_ref().map(|j| j.downloaded.load(Ordering::SeqCst)),
                "total": job.as_ref().map(|j| j.total.load(Ordering::SeqCst)).filter(|t| *t > 0),
            }))
        })(),
        "offlineDownload" => (|| -> Result<Value, String> {
            let id = arg_id(args)?;
            let meta = args.get("meta").cloned().unwrap_or(Value::Null);
            let job = ensure(app, Kind::Offline, &id, &arg_url(args)?, meta)?;
            Ok(json!({ "trackId": id, "started": job.is_some(), "offline": job.is_none() }))
        })(),
        "offlineList" => Ok(json!({ "items": list_offline(&c) })),
        "offlineDelete" => (|| -> Result<Value, String> {
            if args.get("all").and_then(Value::as_bool) == Some(true) {
                for item in list_offline(&c) {
                    if let Some(id) = item.get("trackId").and_then(Value::as_str) {
                        remove_kind(&c, Kind::Offline, id);
                    }
                }
                return Ok(json!({ "deleted": "all" }));
            }
            let id = arg_id(args)?;
            remove_kind(&c, Kind::Offline, &id);
            Ok(json!({ "deleted": id }))
        })(),
        "cacheInfo" => {
            let (on, ob) = c.stats(Kind::Offline);
            let (sn, sb) = c.stats(Kind::Stream);
            Ok(json!({
                "offline": { "count": on, "bytes": ob },
                "stream": { "count": sn, "bytes": sb, "limit": c.limit.load(Ordering::SeqCst) },
                "dir": c.offline_dir.parent().map(|p| p.to_string_lossy().to_string()),
            }))
        }
        "cacheClear" => {
            let busy: Vec<String> = c.jobs.lock().unwrap().keys().filter(|k| k.0 == Kind::Stream).map(|k| k.1.clone()).collect();
            if let Ok(rd) = fs::read_dir(&c.stream_dir) {
                for e in rd.flatten() {
                    let p = e.path();
                    let id = p.file_stem().and_then(|s| s.to_str()).unwrap_or("").to_string();
                    if !busy.contains(&id) && p.extension().and_then(|x| x.to_str()) != Some("part") {
                        let _ = fs::remove_file(&p);
                    }
                }
            }
            Ok(Value::Null)
        }
        "setCacheLimit" => (|| -> Result<Value, String> {
            let bytes = args.get("bytes").and_then(Value::as_u64).ok_or("нужен bytes")?;
            c.limit.store(bytes.max(MIN_LIMIT), Ordering::SeqCst);
            c.save_limit();
            c.evict();
            Ok(json!({ "limit": c.limit.load(Ordering::SeqCst) }))
        })(),
        _ => return None,
    };
    Some(res)
}
