# MoodFlow — Mini Demo Teknik Plan (v1.8) — Veri Kaynakları Tamamlandı

**Karar:** Beş müzik özelliği için öncelikli kaynak **FreqBlog**, yedekler **GetSongBPM / Last.fm / MusicBrainz / yerel ses analizi**. Ses edinme modülünde **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** kullanılacak. Otomatik ses edinme, bölüm seçimi, sıralama, geçiş planlama ve miks render etme demo akışının parçasıdır. Kullanıcı MP3/WAV yüklemeyecek.

## 1. Hedef ve kesin kapsam

Bir kullanıcı herkese açık **YouTube veya Spotify playlist URL'sini** yapıştırır. MoodFlow şarkıları okur, **BPM, Key/Camelot, Mood, Energy, Genre** özelliklerini toplar ve dört moddan biriyle uygulama içinde yeniden sıralar. Ardından **"Otomatik Miks Oluştur"** düğmesiyle seçilen şarkıların YouTube ses kaynaklarını `yt-dlp` üzerinden edinir, müzikal kesitleri ve geçişleri otomatik belirler, kısa miks oluşturur ve tarayıcıda oynatır.

- **Giriş yok:** Kullanıcı hesabı, OAuth, Spotify/Google oturum açma ekranı yok.
- **Kaynak playlist değişmez:** YouTube/Spotify'a yeni sıra yazılmaz, export yok.
- **Sadece public listeler:** Gizli veya erişilemeyen listelerde açık hata gösterilir.
- **Tek makine, tek backend:** React + FastAPI. Veritabanı / Redis / Celery / mikroservis deployment yok.
- **Demo limitleri:** En fazla 20 playlist öğesi analiz edilir; ilk miks en fazla **6** eşleşmiş şarkıdan **yaklaşık 60–100 saniye** olur.
- **Ses kaynağı:** YouTube için doğrudan video ID; Spotify öğeleri için başlık/sanatçıyla YouTube'da eşleşen kayıt aranır. Spotify'ın ses akışı kullanılmaz.
- **Çalışma koşulu:** `yt-dlp` üzerinden indirme ve işleme, kaynağın erişilebilirliği ve gerekli kullanım izinlerine bağlıdır. Erişilemeyen içerikler atlanır ve arayüzde belirtilir.

## 2. Tek ekranlık kullanıcı akışı

```text
[Public YouTube / Spotify playlist URL] [Playlisti Oku]
           ↓
[20 şarkı + BPM / Key / Mood / Energy / Genre]
           ↓
[Daily] [DJ Mix] [Emotional] [Custom]
           ↓
[Orijinal sıra]     [MoodFlow sırası]
           ↓
              [Otomatik Miks Oluştur]
           ↓
[İndirme → Ses analizi → Bölüm seçimi → Geçiş → Render]
           ↓
                 [▶ Miks önizlemesi]
```

İndirme, eşleşme, analiz ve render durumları aynı ekranda gösterilir. Kullanıcı teknik ayar yapmaz.

## 3. Teknoloji

| Parça | Seçim | Görev |
|---|---|---|
| UI | React + TypeScript + Vite | Tek sayfa ve HTML5 ses oynatıcı |
| API | Python 3.11+ + FastAPI | Import, rank, mix endpoint'leri |
| Playlist okuma | `yt-dlp` (YouTube); public Spotify adapter | Kullanıcı girişi olmadan metadata |
| YouTube bulma/indirme | **`yt-dlp` Python `YoutubeDL` API** | Arama, eşleştirme ve ses dosyası edinme |
| Müzik özellikleri | **FreqBlog `/lookup`** | BPM, Key/Camelot, Mood, Energy, Genre + Valence |
| Eksik BPM/Key | **GetSongBPM** | Bulunmayan tempo / ton için alternatif |
| Eksik Genre | **Last.fm `track.getTopTags`** | Şarkı etiketlerinden genre çıkarma |
| Şarkı kimliği | **MusicBrainz** (opsiyonel) | ISRC ve kayıt/sürüm eşleştirme |
| Yerel ses analizi | `librosa` + NumPy | Beat, RMS, uygun kesit / cue |
| Audio render | `ffmpeg` + `ffprobe` | WAV decode, crossfade, gain, çıkış MP3 |
| Kalıcı depolama | Yok | `tmp/` + kısa ömürlü geçici cache |

> **Spotify import notu:** Public playlist okuma için girişsiz üçüncü taraf adapter (örn. SpotAPI) prototipi kullanılacak. Spotify'ın güncel resmî Web API'si rastgele public playlistleri kullanıcı girişi olmadan güvenilir biçimde okumayı garanti etmiyor. Bu nedenle ilk doğrulama adımında gerçekten çalışan bir Spotify public URL'si test edilecek; çalışmazsa uygulama hata gösterecek, kullanıcıyı OAuth'a yönlendirmeyecek. YouTube akışı tek başına demo sunabilmelidir.

## 4. Proje yapısı

```text
moodflow/
├── frontend/
│   └── src/
│       ├── App.tsx              # Tek ekran
│       └── api.ts
├── backend/
│   ├── main.py                  # FastAPI endpoint'leri
│   ├── importers.py             # YouTube + Spotify public metadata
│   ├── features.py              # Beş özellik + sağlayıcı önceliği ve fallback
│   ├── feature_providers.py     # FreqBlog, GetSongBPM, Last.fm adaptörleri
│   ├── ranking.py               # Dört mod, tek optimizer
│   ├── audio_sources.py         # yt-dlp indirme, Spotify→YouTube match
│   ├── arrangement.py           # Otomatik kesit ve geçiş planı
│   ├── mixing.py                # ffmpeg render
│   └── schemas.py               # Pydantic kontratlar
├── tests/
│   ├── test_importers.py
│   ├── test_ranking.py
│   ├── test_audio_sources.py
│   └── test_mixing.py
├── fixtures/sample_playlist.json
├── .env.example                # Sunucu tarafı harici API anahtarları
└── README.md
```

**Modülerlik:** Her dosya bir sorumluluğa sahip. Sonradan servis ayırmak istenirse dış arayüzler korunur; bugün ayrı süreçler kurmuyoruz.

## 5. Playlist import ve şarkı eşleştirme

### YouTube

- Playlist URL'sinden `list` parametresini çıkar.
- `yt_dlp.YoutubeDL({'extract_flat': 'in_playlist', 'skip_download': True, 'quiet': True})` ile liste bilgilerini al; yalnızca ilk 20 öğeyi işle.
- `id`, başlık, kanal/artist, süre, kaynak URL ve orijinal sırasını sakla.
- Resmî YouTube API anahtarı kullanıcıdan istenmez; `yt-dlp` public metadata çıkarma yaklaşımı kullanılır.

### Spotify

- Public Spotify URL'sindeki playlist kimliğini çıkar.
- `SpotifyPublicAdapter` ile şarkı adı, sanatçı, süre ve listedeki sırasını edin.
- Doğrudan Spotify sesi alma. Miks aşamasında her parçaya **YouTube araması** yap.

### Spotify → YouTube kaynak bulma

1. `yt-dlp` ile `ytsearch3:<artist> - <track> audio` üzerinden en fazla üç aday getir.
2. Normalize edilmiş sanatçı/şarkı adı, süre farkı ve `live`, `cover`, `remix`, `slowed` gibi sürüm etiketlerine göre skorla.
3. Eşleşme güvenilir değilse **yanlış parçayı otomatik indirme**; `unmatched` olarak göster ve sıradaki parçaya geç.
4. YouTube playlist öğelerinde mevcut `video_id` kullan; gereksiz yeniden arama yapma.

### Normalize şarkı modeli (örnek)

```json
{
  "occurrence_id": "item-004",
  "platform": "spotify",
  "source_id": "spotify-track-id",
  "title": "Example Song",
  "artist": "Example Artist",
  "duration_sec": 210,
  "original_position": 4,
  "features": {
    "bpm": 124,
    "camelot": "8A",
    "mood": ["melancholic"],
    "valence": 0.35,
    "energy": 0.72,
    "genre": ["synthpop"]
  },
  "youtube_video_id": null,
  "match_status": "pending"
}
```

Bilinmeyen özellikler `null` olmalı. Her listedeki öğe kendi `occurrence_id` değerini korur; tekrar eden şarkılar kaybolmaz.

## 6. Beş özelliğin gerçek veri kaynakları ve otomatik sıralama

Bu bölüm **zorunlu uygulama sözleşmesidir**. API sağlayıcısı belirsiz bırakılamaz. Playlist importu **şarkı adı/sanatçı/süre/platform ID** verir; BPM/Key/Mood/Energy/Genre değerlerini kendi kendine sağlamaz. `features.py` bu bilgilerle dış kaynakları sorgular. **Kullanıcı auth yoktur; yalnızca geliştiriciye ait API anahtarları sunucuda bulunur.**

### 6.1 Kaynak önceliği

| Özellik | Birincil | Yedek | Ek not |
|---|---|---|---|
| **BPM** | FreqBlog `/lookup` → `bpm` | GetSongBPM `/search/` + `/song/` → `tempo`; ses mevcutsa librosa beat tracking | Half/double-time için `bpm_alt` da saklanır |
| **Key / Camelot** | FreqBlog `/lookup` → `key`, `camelot` | GetSongBPM `/song/` → `key_of`, `open_key`; gerekirse yerel chroma tahmini | GetSongBPM Open Key → Camelot dönüşümü gerekir |
| **Mood** | FreqBlog `/lookup` → `mood`, `valence` | Sadece güvenilir valence + energy varsa *türetilmiş* kaba mood; aksi hâlde `null` | Genel enerji, duygu ile aynı şey değildir |
| **Energy** | FreqBlog `/lookup` → `energy` | İndirilen sesten librosa RMS → playlist içi normalize değer | Yerel RMS ile sağlayıcı enerji ölçeği doğrudan aynı kabul edilmez |
| **Genre** | FreqBlog `/lookup` → `genre` | Last.fm `track.getTopTags`; MusicBrainz kayıt/sanatçı etiketleri | Last.fm etiketleri genre dışı olabilir; filtrele |

**Not:** Birinci kaynak beş özelliği tek çağrıda döndürebilir; üç yedek servisi her şarkı için peşinen çağırma. Yalnız eksik veya şüpheli alanlarda fallback uygula.

### 6.2 Birincil API — FreqBlog

- **Base URL:** `https://api.freqblog.com`
- **Kimlik:** Sunucu tarafında `X-Api-Key: <FREQBLOG_API_KEY>`
- **Endpoint:** `GET /lookup?track=<şarkı>&artist=<sanatçı>`
- **API dokümanı:** https://api.freqblog.com/docs
- **Alanlar:** `track_name`, `artist_name`, `isrc`, `duration_ms`, `bpm`, `bpm_alt`, `bpm_confidence`, `key`, `key_confidence`, `camelot`, `mood`, `valence`, `energy`, `genre`, `feature_source`
- **Kota:** Belgelenen ücretsiz plan ayda 1.000 ücretlendirilen sorgu; 20 şarkılık bir playlist yaklaşık 20 `/lookup` sorgusu gerektirir. Limitler değişebilir.
- **Önemli:** Katalogda olmayan parçalar `202 Processing` dönebilir; birkaç defa kontrollü yeniden dene. `429` gelirse `Retry-After` süresini uygula. Yanıtın eşleşen şarkı/sanatçı/süre alanlarını kaynakla kontrol et.
- **Toplu sorgu:** `/bulk` 50 parçaya kadar destekler; ilk basit implementasyonda tekil `/lookup` çağrıları yeterli. İleride kota ve gecikme optimizasyonu için bulk kullanılabilir.

**Kontrol amaçlı cURL:**

```bash
curl -G 'https://api.freqblog.com/lookup' \
  --data-urlencode 'track=Blinding Lights' \
  --data-urlencode 'artist=The Weeknd' \
  -H "X-Api-Key: $FREQBLOG_API_KEY"
```

**Minimal Python provider (başarılı yanıtın normalize edilmesi):**

```python
import os
import httpx

async def fetch_freqblog(title: str, artist: str) -> dict | None:
    async with httpx.AsyncClient(timeout=20.0) as client:
        res = await client.get(
            "https://api.freqblog.com/lookup",
            params={"track": title, "artist": artist},
            headers={"X-Api-Key": os.environ["FREQBLOG_API_KEY"]},
        )
    if res.status_code in (202, 404):
        return None  # 202 için çağıran katman kontrollü retry yapar
    res.raise_for_status()  # 429 için çağıran katman Retry-After uygular
    x = res.json()
    return {
        "bpm": x.get("bpm"),
        "bpm_alt": x.get("bpm_alt"),
        "camelot": x.get("camelot"),
        "key": x.get("key"),
        "mood": x.get("mood"),
        "valence": x.get("valence"),
        "energy": x.get("energy"),
        "genre": [x["genre"]] if x.get("genre") else [],
        "feature_source": x.get("feature_source", "freqblog"),
        "bpm_confidence": x.get("bpm_confidence"),
        "key_confidence": x.get("key_confidence"),
        "matched_title": x.get("track_name"),
        "matched_artist": x.get("artist_name"),
        "matched_duration_ms": x.get("duration_ms"),
    }
```

### 6.3 Yedek sağlayıcılar — yalnız eksik alanlar

**GetSongBPM / GetSongKEY:**

- Base URL: `https://api.getsong.co/` ; `X-API-KEY: <GETSONGBPM_API_KEY>`
- **Arama:** `GET /search/?type=both&lookup=song:<TITLE> artist:<ARTIST>`
- **Bulunan şarkı:** `GET /song/?id=<GETSONG_SONG_ID>`
- `tempo` BPM olarak kullanılır; `key_of` ve `open_key` ton için kullanılır.
- Resmî referans: https://getsongbpm.com/api ; ücretsiz erişimde kaynak bağlantısı (backlink) koşulu vardır. API anahtarı yoksa fallback atlanır.

**Last.fm (genre etiketleri):**

- Endpoint: `GET https://ws.audioscrobbler.com/2.0/?method=track.getTopTags&artist=<ARTIST>&track=<TITLE>&api_key=<LASTFM_API_KEY>&format=json`
- `toptags.tag[].name` ve `count` döner. `rock`, `house`, `synthpop` gibi tür etiketlerini al; `seen live`, `favorite` gibi tür olmayanları filtrele.
- Bu metodun **kullanıcı oturumu gerekmiyor**, yalnız API key gerekir.
- Resmî referans: https://www.last.fm/api/show/track.getTopTags

**MusicBrainz (opsiyonel eşleştirme):**

- Endpoint: `GET https://musicbrainz.org/ws/2/recording/?query=recording:<TITLE> AND artist:<ARTIST>&fmt=json`
- `recording` ve ISRC eşleştirmesinde kullanılabilir. BPM/energy/mood kaynağı **değildir**. `User-Agent` ile uygulama tanıtımı yap; MusicBrainz kullanım sınırlarını uygula.
- Resmî referans: https://musicbrainz.org/doc/MusicBrainz_API

### 6.4 İndirilmiş sesten yerel doğrulama

`yt-dlp` ile ses dosyası edinilebildiğinde sadece miks için ek analiz çalıştır:

- `librosa.beat.beat_track(...)`: Ses kaydının yaklaşık BPM/beat-grid ölçümü; özellikle live/remix versiyonlarını kontrol et.
- `librosa.feature.rms(...)`: Kesit enerji profili ve cue point arama. Birincil API `energy` değerini otomatik geçersiz kılma; farklı ölçeklerde olabilirler.
- `librosa.feature.chroma_stft(...)`: Ton tahmini için girdidir; doğrudan güvenilir key veya Camelot etiketi değildir. Demo kapsamında key tahminini ayrıca doğrulamadan kullanma.
- **Mood ve genre**, yalnız RMS/BPM'den güvenilir biçimde üretilmez. Bu değerler bulunamazsa `null` veya boş liste olarak kalır.

### 6.5 Eşleştirme, tutarlılık ve hata politikası

1. Spotify/YouTube metadata'sından `title`, `artist`, `duration_sec` ve `version` (`live`, `remix`, `cover`, `slowed`) çıkar.
2. FreqBlog lookup sonucu doğru sanatçı ve sürüme ait mi kontrol et. Süre biliniyorsa belirgin farkı şüpheli say; örnek ilk eşik **±20 sn** (ayar dosyasında değişebilir).
3. Eksik BPM/Key için GetSongBPM; eksik genre için Last.fm çağır. `null` değerleri kendiliğinden doldurma.
4. Her alan için `source`, `confidence` (gerçek kaynak veriyorsa), `status` tut. `bpm_confidence` FreqBlog'da 0–1 skala **değildir**; `key_confidence` ise 0–1'dir. Aynı confidence alanına körlemesine koyma.
5. Başarısız API çağrısı tek şarkının işlenmesini engellemesin. `missing_features` listesiyle arayüzde göster.
6. `features.py` çıktılarını bellek cache'inde `artist + title + version` anahtarıyla sakla; tekrar mod değiştirirken API çağırma.
7. Ses indirildiğinde müzik versiyonu farklı çıkarsa, özellikle DJ Mix'e başlamadan önce BPM/key verisini o kayda göre yeniden kontrol et.

**Normalize edilmiş feature JSON örneği (değerler temsili):**

```json
{
  "bpm": 124.0,
  "bpm_alt": 62.0,
  "key": "A-Minor",
  "camelot": "8A",
  "mood": "melancholic",
  "valence": 0.34,
  "energy": 0.71,
  "genre": ["synthpop"],
  "sources": {"bpm": "freqblog", "key": "freqblog", "mood": "freqblog", "energy": "freqblog", "genre": "lastfm"},
  "missing_features": []
}
```

### 6.6 Bu veriler sıralamaya nasıl giriyor?

| Özellik | Daily | DJ Mix | Emotional |
|---|---:|---:|---:|
| BPM | 20 | 30 | 15 |
| Key | 10 | 30 | 5 |
| Mood | 20 | 10 | 30 |
| Energy | 25 | 20 | 30 |
| Genre | 25 | 10 | 20 |

- **Daily:** Genel uyumu ve tür sürekliliğini dengeler.
- **DJ Mix:** Tempo ve harmonik geçişleri öncelemeli.
- **Emotional:** Enerji ve duygu için `sakin → zirve → sakin` hedef eğrisi uygular.
- **Custom:** Beş slider'dan gelen ağırlıkları normalize eder.

İki şarkı arasındaki geçiş maliyeti: `C(i,j) = Σ w_f × d_f(i,j)`; her `d_f` değeri `0..1` aralığında. BPM oransal uzaklık, key Camelot komşuluğu, mood etiket/valence, energy mutlak fark, genre etiket benzerliği kullanır. Eksik veriler için mevcut ağırlıklar yeniden normalize edilir.

**Optimizasyon:** 3–5 greedy başlangıç + `swap` / `relocate` iyileştirme. En düşük maliyetli sırayı seç. Orijinal sıraya kıyasla daha kötü bir sonuç çıkarsa orijinali tut. Sıralama yalnızca bellekte gerçekleşir.

## 7. yt-dlp ile otomatik ses edinme — zorunlu

`audio_sources.py` **yt-dlp Python API** üzerinden çalışır. `os.system`, shell string birleştirme, manuel MP3 yükleme veya tarayıcı indirme akışı yok.

```bash
python -m pip install -U yt-dlp
# ffmpeg ve ffprobe sistemde ayrıca kurulu olmalı
```

```python
from pathlib import Path
from yt_dlp import YoutubeDL


def fetch_audio(video_id: str, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    wav = destination / f"{video_id}.wav"
    if wav.is_file():
        return wav
    options = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "outtmpl": str(destination / f"{video_id}.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "wav"
        }],
        "quiet": True,
    }
    with YoutubeDL(options) as ydl:
        ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
    if not wav.is_file():
        raise RuntimeError(f"Ses alınamadı: {video_id}")
    return wav
```

Bu minimal örnekte `video_id` doğrulama, URL/yol güvenliği, eşzamanlılık limiti, zaman aşımı, geçici disk limiti ve hata loglama çağıran katmanda eklenecek. Gerçek akışta `video_id` yalnızca doğrulanmış YouTube eşleşmesinden alınır.

**İndirme politikası:** Miks için optimize edilmiş sıradan ilk 6 uygun şarkıyı seç. Her dosyayı `video_id` ile cache'le, tekrar indirme. Bir video başarısız olursa tüm iş akışını durdurma; başka bir uygun listedeki parçayı dene. En az iki ses dosyası olmadan miks render etme. Giriş, cookie veya kullanıcı hesabı talep etme.

## 8. Otomatik miksaj ve düzenleme

Kullanıcı yalnızca **"Otomatik Miks Oluştur"** düğmesine basar.

1. **Decode:** Sesleri `ffmpeg` ile aynı örnekleme hızına (44.1 kHz), stereo PCM'e çevir.
2. **Kesit belirleme:** `librosa` ile RMS/beat çıkar. Uzun sessiz veya çok düşük enerjili bölgeleri atla. Her parçadan **yaklaşık 15–20 saniye** kesit seç. Beat/phrase sınırına yaklaşmaya çalış; rastgele saniye kesme.
3. **Cue planı:** Segment başlangıcı, süresi, geçiş zamanı ve türünü JSON'a yaz.
4. **Geçiş seçme:** BPM yakını ve ritim tahmini güvenilir ise vuruşa yakın **3–6 saniye crossfade**; değilse **1–3 saniyelik güvenli fade** uygula.
5. **Ses seviyesi:** Segmentleri algılanan seviyeye göre normalize et; ani ses sıçraması ve clipping için limiter uygula.
6. **Render:** FFmpeg ile segmentleri birleştir; demo için tek `preview.mp3` üret (yaklaşık 60–100 sn).
7. **Dinletme:** Backend `audio_url` döndürür; frontend HTML5 player ile oynatır.

**Demo hedefi:** Beat-aware kesit seçim ve adaptif fade. Stems, gelişmiş EQ, kusursuz profesyonel tempo senkronizasyonu ve tam uzunlukta DJ seti yok.

### Düzenleme planı (örnek)

```json
{
  "segments": [
    {"id":"item-004","start_sec":42,"duration_sec":18},
    {"id":"item-001","start_sec":66,"duration_sec":18}
  ],
  "transitions": [
    {"from":"item-004","to":"item-001","type":"beat_crossfade","duration_sec":4}
  ]
}
```

Bu JSON kullanıcıya gösterilmek zorunda değil; test ve hata ayıklama içindir. İndirme ve miks çıktıları geçici dizinde tutulur, süre sonunda silinir.

## 9. Minimal backend API

```text
POST /api/import
  { "url": "https://.../playlist..." }
  -> { "tracks": [...], "warnings": [...] }

POST /api/rank
  { "tracks": [...], "mode": "daily|dj|emotional|custom", "weights": {...} }
  -> { "ordered_ids": [...], "score_before": 0.68, "score_after": 0.42 }

POST /api/mix
  { "tracks": [...], "ordered_ids": [...] }
  -> { "mix_id": "...", "status": "processing" }

GET /api/mix/{mix_id}
  -> { "status": "processing|done|failed", "audio_url": "/api/audio/...", "warnings": [...] }

GET /api/audio/{mix_id}
  -> audio/mpeg
```

`/api/mix` arka planda FastAPI `BackgroundTasks` veya sınırlı iş yürütücüsü ile çalışabilir. Tek kullanıcı demosunda yeterli; tarayıcı `/api/mix/{mix_id}` endpoint'ini poll eder. Çok kullanıcılı, kalıcı kuyruk gerekmiyor.

## 10. Geliştirme sırası

1. **YouTube + Spotify importer:** Public YouTube listesi çalışsın; Spotify için girişsiz adapter'ın gerçek erişilebilirliğini test et. Sorun varsa kullanıcıya net hata göster.
2. **Feature providers:** FreqBlog `/lookup` ile beş özelliği gerçekten çek. Eksikler için GetSongBPM / Last.fm fallback yap, eşleşme kontrolünü ve 202/429 hata yönetimini test et.
3. **Ranking:** Beş özellik, dört mod, önce/sonra sıralaması ve skor.
4. **yt-dlp ses edinme:** YouTube video ID ile indirme, Spotify parçası için YouTube eşleştirme, cache, hata yönetimi.
5. **Auto Arrangement + Mixing:** 6 parçadan otomatik kesit + geçiş + ses seviyesi + MP3 render.
6. **React tek ekran:** Playlist yapıştır → mod seç → sırala → otomatik miks oluştur → dinle.
7. **Temel testler:** Hatalı URL, gizli/erişilemeyen liste, eksik metadata, başarısız indirme, yanlış eşleşme, tekrar eden parça, bozuk ses, eksik iki kaynak, clipping ve istemsiz sessizlik.

## 11. Demo kabul kriterleri

- [ ] Kullanıcı giriş yapmadan public YouTube playlistini okur (ilk 20 öğe).
- [ ] Public Spotify playlist okuma başarılıysa gösterilir; mümkün değilse OAuth istemeden net hata verir.
- [ ] En az bir gerçek test parçasında FreqBlog `/lookup` çağrısı yapılır; `bpm`, `key/camelot`, `mood`, `energy`, `genre` alanları normalize edilir.
- [ ] Eksik alanlara GetSongBPM/Last.fm fallback uygulanır; başarısız sağlayıcı nedeniyle uygulama çökmez.
- [ ] BPM, Key, Mood, Energy, Genre gösterilir; eksik veri uydurulmaz.
- [ ] Dört modda şarkılar yerel olarak sıralanır, orijinal playlist değişmez.
- [ ] `yt-dlp` gerçekten backend'de kullanılır; MP3/WAV yükleme yoktur.
- [ ] Spotify'dan gelen eşleşmiş şarkılar için YouTube kaynağı otomatik bulunur.
- [ ] Altı uygun sesten yaklaşık 60–100 saniyelik geçişli, ses seviyesi dengelenmiş miks çıkar.
- [ ] Kullanıcı miksin tamamını sayfada dinler; hata varsa anlaşılır mesaj alır.
- [ ] Tüm demo localhost üzerinde `README.md` komutlarıyla çalışır.

**Teslim tanımı:** Tek sayfa, tek FastAPI backend, 4 profil, 5 feature, `yt-dlp` tabanlı otomatik ses edinme ve FFmpeg tabanlı otomatik miksaj. Başka altyapı veya özellik ekleme.

## 12. `.env.example` — gerekli API anahtarları

```dotenv
# Birincil müzik metadata kaynağı (geliştirici anahtarı, kullanıcı auth değil)
FREQBLOG_API_KEY=

# Sadece eksik BPM/ton ve genre için fallback; boşsa atlanır
GETSONGBPM_API_KEY=
LASTFM_API_KEY=

# YouTube playlist okuma + yt-dlp indirimi için kullanıcı OAuth gerekmiyor
# Spotify public adapter deneysel; giriş ekranı eklenmeyecek
```

`features.py` anahtarı environment'tan okur; frontend'e geri göndermez. Anahtarsız demo için `fixtures/sample_playlist.json` ile örnek sıralama gösterilebilir, fakat gerçek metadata başarı testi olarak sayılmaz.

### Kaynak dokümanları (22 Kasım 2026 kontrolü)

- FreqBlog API: https://freqblog.com/ ; https://api.freqblog.com/docs
- GetSongBPM API: https://getsongbpm.com/api
- Last.fm `track.getTopTags`: https://www.last.fm/api/show/track.getTopTags
- MusicBrainz: https://musicbrainz.org/doc/MusicBrainz_API
- yt-dlp Python embedding: https://github.com/yt-dlp/yt-dlp#embedding-yt-dlp
