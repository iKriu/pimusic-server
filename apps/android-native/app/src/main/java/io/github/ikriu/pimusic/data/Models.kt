package io.github.ikriu.pimusic.data

import kotlinx.serialization.KSerializer
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.descriptors.PrimitiveKind
import kotlinx.serialization.descriptors.PrimitiveSerialDescriptor
import kotlinx.serialization.encoding.Decoder
import kotlinx.serialization.encoding.Encoder
import kotlinx.serialization.json.JsonDecoder
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull

/** Строка, которую сервер может прислать и числом (id каталога, год «2016»). */
object LooseString : KSerializer<String?> {
    override val descriptor = PrimitiveSerialDescriptor("LooseString", PrimitiveKind.STRING)
    override fun deserialize(decoder: Decoder): String? {
        val el = (decoder as? JsonDecoder)?.decodeJsonElement() ?: return decoder.decodeString()
        return (el as? JsonPrimitive)?.takeIf { it !is JsonNull }?.contentOrNull?.takeIf { it.isNotEmpty() }
    }
    override fun serialize(encoder: Encoder, value: String?) {
        if (value == null) encoder.encodeNull() else encoder.encodeString(value)
    }
}

// ---------------------------------------------------------------- треки медиатеки / плеера (PLAYER_API.md)

@Serializable
data class Track(
    val id: Long,
    val artist: String = "",
    val title: String = "",
    val album: String = "",
    val duration: String = "",
    val seconds: Long = 0,
    val cover: String? = null,
    val stream: String? = null,
    val owned: Boolean = true,
    val gain: Double? = null,
    val peak: Double? = null,
    val genre: String? = null,
    val year: Int? = null,
    @SerialName("added_at") val addedAt: Long? = null,
    @SerialName("item_id") val itemId: Long? = null,
    @SerialName("added_by") val addedBy: String? = null,
    @SerialName("artist_key") val artistKey: String? = null,
    @SerialName("album_key") val albumKey: String? = null,
    /** файл помечен проблемным: перекачка идёт или не удалась (POST /api/tracks/{id}/redownload) */
    val broken: Boolean = false,
    /** размер файла, байт (null — файла нет): оценка «Скачать на устройство» */
    val size: Long? = null,
)

@Serializable
data class TrackList(val items: List<Track> = emptyList(), val total: Int = 0)

@Serializable
data class LibraryPage(
    val total: Int = 0,
    @SerialName("library_total") val libraryTotal: Int = 0,
    val offset: Int = 0,
    val limit: Int = 50,
    val items: List<Track> = emptyList(),
)

@Serializable data class Genre(val name: String, val count: Int = 0)
@Serializable data class Genres(val items: List<Genre> = emptyList())

@Serializable
data class LibArtist(
    val key: String,
    val name: String = "",
    val count: Int = 0,
    val year: Int? = null,
    @SerialName("added_at") val addedAt: Long? = null,
    val image: String? = null,
)

@Serializable data class LibArtists(val items: List<LibArtist> = emptyList())

@Serializable
data class LibAlbum(
    @SerialName("artist_key") val artistKey: String,
    val key: String,
    val title: String = "",
    val artist: String = "",
    val count: Int = 0,
    val year: Int? = null,
    @SerialName("added_at") val addedAt: Long? = null,
    val cover: String? = null,
)

@Serializable data class LibAlbums(val items: List<LibAlbum> = emptyList())

@Serializable
data class AlbumGroup(val key: String, val title: String = "", val cover: String? = null, val tracks: List<Track> = emptyList())

@Serializable
data class LibArtistPage(
    val key: String,
    val name: String = "",
    val count: Int = 0,
    val image: String? = null,
    val albums: List<AlbumGroup> = emptyList(),
)

@Serializable
data class LibAlbumPage(
    val key: String,
    val title: String = "",
    val cover: String? = null,
    val tracks: List<Track> = emptyList(),
    @SerialName("artist_key") val artistKey: String = "",
    val artist: String = "",
)

@Serializable data class LibKeys(val keys: List<String> = emptyList(), val isrc: List<String> = emptyList())

// ---------------------------------------------------------------- плейлисты

@Serializable data class Member(val login: String)
@Serializable data class Invite(val token: String, val link: String? = null, @SerialName("app_link") val appLink: String? = null)

@Serializable
data class Playlist(
    val id: Long,
    val name: String = "",
    val description: String = "",
    val count: Int = 0,
    val seconds: Long = 0,
    val covers: List<String> = emptyList(),
    val shared: Boolean = false,
    @SerialName("invite_enabled") val inviteEnabled: Boolean = false,
    val role: String = "owner",
    val owner: String = "",
    val members: List<Member> = emptyList(),
    @SerialName("updated_at") val updatedAt: Long = 0,
    val invite: Invite? = null,
    val tracks: List<Track>? = null,
)

@Serializable data class Playlists(val items: List<Playlist> = emptyList())
@Serializable data class ForTrackItem(val id: Long, val name: String = "", val has: Boolean = false)
@Serializable data class ForTrack(val items: List<ForTrackItem> = emptyList())
@Serializable data class AddResult(val added: Int = 0, val skipped: Int = 0, val count: Int = 0, val full: Boolean = false)
@Serializable data class SaveResult(val linked: Int = 0, val missing: Int = 0, val failed: Int = 0)
@Serializable data class JoinPreview(val id: Long, val name: String = "", val owner: String = "", val count: Int = 0, val covers: List<String> = emptyList(), val already: Boolean = false)

// ---------------------------------------------------------------- поиск и каталог

@Serializable
data class CTrack(
    val i: Int,
    val artist: String = "",
    val title: String = "",
    val album: String? = null,
    val duration: String = "",
    val key: String? = null,
    val isrc: String? = null,
    val cover: String? = null,
    val preview: String? = null,
    @Serializable(with = LooseString::class) val year: String? = null,
    val no: Int? = null,
)

@Serializable
data class CAlbum(
    val i: Int = 0,
    @Serializable(with = LooseString::class) val id: String? = null,
    val title: String = "",
    @Serializable(with = LooseString::class) val year: String? = null,
    val kind: String = "album",
    val artist: String = "",
    @Serializable(with = LooseString::class) @SerialName("artist_id") val artistId: String? = null,
    val cover: String? = null,
)

@Serializable
data class CArtist(
    @Serializable(with = LooseString::class) val id: String? = null,
    val name: String = "",
    val picture: String? = null,
    val fans: Long = 0,
)

@Serializable
data class SearchResult(
    val token: String,
    val artist: String? = null,
    @Serializable(with = LooseString::class) @SerialName("artist_id") val artistId: String? = null,
    val picture: String? = null,
    val fans: Long = 0,
    val tracks: List<CTrack> = emptyList(),
    val albums: List<CAlbum> = emptyList(),
    val artists: List<CArtist> = emptyList(),
)

@Serializable
data class CatalogAlbum(
    val token: String,
    @Serializable(with = LooseString::class) val id: String? = null,
    val title: String = "",
    val artist: String = "",
    @Serializable(with = LooseString::class) @SerialName("artist_id") val artistId: String? = null,
    @Serializable(with = LooseString::class) val year: String? = null,
    val kind: String = "album",
    val cover: String? = null,
    val tracks: List<CTrack> = emptyList(),
)

@Serializable
data class CatalogArtist(
    val token: String,
    @Serializable(with = LooseString::class) val id: String? = null,
    val name: String = "",
    val picture: String? = null,
    val fans: Long = 0,
    val tracks: List<CTrack> = emptyList(),
    val albums: List<CAlbum> = emptyList(),
)

@Serializable
data class SearchAlbum(val i: Int = 0, val title: String = "", val artist: String = "", @Serializable(with = LooseString::class) val year: String? = null, val tracks: List<CTrack> = emptyList())

@Serializable data class PlRef(val id: Long, val name: String = "", @SerialName("added_now") val addedNow: Int = 0)

@Serializable
data class DownloadResp(val queued: Int = 0, val title: String = "", val kind: String? = null, val note: String? = null, val playlist: PlRef? = null)

// ---------------------------------------------------------------- загрузки, импорт

@Serializable data class Job(val title: String = "", val total: Int = 0, val started: Long = 0)
@Serializable data class LogLine(val ts: Long = 0, val text: String = "")
@Serializable
data class Activity(
    val active: List<String> = emptyList(),
    val jobs: List<Job> = emptyList(),
    val log: List<LogLine> = emptyList(),
    val queue: Int = 0,
    @SerialName("active_items") val activeItems: List<ActiveItem> = emptyList(),
)

/** Что качается сейчас: track_id — только у перекачки; progress — последний шаг («Soulseek: …»). */
@Serializable
data class ActiveItem(val text: String = "", val artist: String? = null, val title: String? = null, @SerialName("track_id") val trackId: Long? = null, val progress: String? = null)

@Serializable
data class Import(val id: Long, val filename: String = "", val total: Int = 0, val state: String = "", val counts: JsonObject? = null, val text: String = "")
@Serializable data class Imports(val imports: List<Import> = emptyList())

@Serializable
data class Upload(
    val id: Long,
    val name: String = "",
    val state: String = "",
    @SerialName("state_text") val stateText: String = "",
    val artist: String? = null,
    val title: String? = null,
    val album: String? = null,
    val duration: Double? = null,
    val size: Long? = null,
    val note: String? = null,
    @SerialName("created_at") val createdAt: Long = 0,
    val track: Track? = null,
)

@Serializable data class Uploads(val items: List<Upload> = emptyList())
@Serializable data class Rejected(val name: String = "", val error: String = "", val message: String = "")
@Serializable
data class UploadResp(val uploads: List<Upload> = emptyList(), val rejected: List<Rejected> = emptyList(), val error: String? = null, val message: String? = null)

@Serializable
data class ImportFileResp(@SerialName("import_id") val importId: Long = 0, val format: String = "", val total: Int = 0, val name: String = "", val playlist: PlRef? = null)

// ---------------------------------------------------------------- профиль, волна, текст, настройки, вход

@Serializable
data class Me(val login: String, val tracks: Int = 0, @SerialName("stream_url") val streamUrl: String? = null, @SerialName("is_admin") val isAdmin: Boolean = false)

@Serializable
data class WaveNow(
    val live: Boolean = false,
    val listeners: Int = 0,
    val artist: String? = null,
    val title: String? = null,
    val album: String? = null,
    val duration: Long? = null,
    @SerialName("started_at") val startedAt: Long? = null,
    val cover: String? = null,
    @SerialName("track_id") val trackId: Long? = null,
)

@Serializable data class WaveDiscover(val seeds: JsonElement? = null, val queued: List<String> = emptyList())
@Serializable data class WaveRefresh(val tracks: Int = 0)

@Serializable data class LyricLine(val t: Double, val text: String = "")
/** status — только у POST /api/tracks/{id}/lyrics/refetch: updated | same | cleared | not_found. */
@Serializable data class Lyrics(val synced: List<LyricLine>? = null, val plain: String? = null, val source: String? = null, val status: String? = null)

@Serializable data class SettingItem(val value: JsonElement? = null, @SerialName("updated_at") val updatedAt: Long = 0)
@Serializable data class SettingsAll(val items: Map<String, SettingItem> = emptyMap())
@Serializable data class SettingPut(val key: String = "", val value: JsonElement? = null, @SerialName("updated_at") val updatedAt: Long = 0)

@Serializable
data class AuthStart(val nonce: String, val code: String? = null, val link: String? = null, @SerialName("expires_in") val expiresIn: Int = 300)
@Serializable
data class AuthPoll(val status: String, val token: String? = null, val login: String? = null, val message: String? = null)

@Serializable
data class Device(
    val id: Long,
    val name: String = "",
    val platform: String = "",
    @SerialName("created_at") val createdAt: Long = 0,
    @SerialName("last_seen") val lastSeen: Long = 0,
    val current: Boolean = false,
)

@Serializable data class Devices(val items: List<Device> = emptyList())
@Serializable data class Ok(val ok: Boolean = false, val revoked: JsonElement? = null, val text: String? = null, val gone: Boolean = false)
@Serializable
data class LibDeleteResp(val deleted: Int = 0, val skipped: Int = 0, @SerialName("deleted_ids") val deletedIds: List<Long> = emptyList(), @SerialName("gone_ids") val goneIds: List<Long> = emptyList())
