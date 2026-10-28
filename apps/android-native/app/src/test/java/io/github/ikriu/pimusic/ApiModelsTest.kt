package io.github.ikriu.pimusic

import io.github.ikriu.pimusic.data.Activity
import io.github.ikriu.pimusic.data.CatalogAlbum
import io.github.ikriu.pimusic.data.LibraryPage
import io.github.ikriu.pimusic.data.Lyrics
import io.github.ikriu.pimusic.data.PiJson
import io.github.ikriu.pimusic.data.Playlist
import io.github.ikriu.pimusic.data.SearchResult
import io.github.ikriu.pimusic.data.SettingsAll
import io.github.ikriu.pimusic.data.UploadResp
import io.github.ikriu.pimusic.player.EqController
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Разбор ответов API (форматы PLAYER_API.md / APPS_API.md и webapp.py). */
class ApiModelsTest {
    @Test fun libraryPageWithTrackFields() {
        val p = PiJson.decodeFromString(LibraryPage.serializer(), """
            {"total":2,"library_total":10,"offset":0,"limit":50,"sort":"added","items":[
             {"id":12,"artist":"Кино","title":"Группа крови","album":"Группа крови","duration":"4:46","seconds":286,
              "cover":"/api/cover/12?u=a&s=b&e=1","stream":"/api/stream/12?u=a&e=1&s=c","owned":true,"gain":-3.2,"peak":0.98,
              "genre":"Rock","year":1988,"added_at":1760000000,"broken":true,"future_field":{"x":1}},
             {"id":13,"title":"Без исполнителя","cover":null,"gain":null,"year":null,"added_at":null}]}
        """)
        assertEquals(2, p.total)
        assertEquals(10, p.libraryTotal)
        val t = p.items[0]
        assertEquals(286L, t.seconds)
        assertEquals(-3.2, t.gain!!, 0.0)
        assertEquals(1988, t.year)
        assertTrue(t.broken)
        assertNull(p.items[1].cover)
        assertEquals("", p.items[1].artist)
        assertFalse(p.items[1].broken)
    }

    @Test fun playlistWithTracksAndInvite() {
        val p = PiJson.decodeFromString(Playlist.serializer(), """
            {"id":3,"name":"Дорога","description":"","count":1,"seconds":200,"covers":["/api/cover/1"],"shared":true,
             "invite_enabled":true,"role":"owner","owner":"iKriu","members":[{"login":"Pastur"}],"updated_at":1760000000,
             "invite":{"token":"tok","link":null,"app_link":"https://music.example.com/app/#join=tok"},"sort":"manual",
             "tracks":[{"id":1,"title":"T","item_id":55,"added_by":"Pastur","owned":false}]}
        """)
        assertEquals("tok", p.invite!!.token)
        assertNull(p.invite!!.link)
        assertEquals(55L, p.tracks!![0].itemId)
        assertFalse(p.tracks!![0].owned)
        assertEquals("Pastur", p.members[0].login)
    }

    @Test fun catalogLooseYearAndIds() {
        val s = PiJson.decodeFromString(SearchResult.serializer(), """
            {"token":"t1","artist":"A","artist_id":123,"picture":null,"fans":5,
             "tracks":[{"i":0,"artist":"A","title":"S","album":"Al","duration":"3:00","key":"a|s","isrc":null,"cover":"https://x/c.jpg","preview":null}],
             "albums":[{"i":0,"id":456,"title":"Al","year":"2016","kind":"album","artist":"A","artist_id":"123","cover":null}],
             "artists":[{"id":123,"name":"A","picture":null,"fans":5}]}
        """)
        assertEquals("123", s.artistId)
        assertEquals("456", s.albums[0].id)
        assertEquals("2016", s.albums[0].year)
        assertEquals("a|s", s.tracks[0].key)
        val a = PiJson.decodeFromString(CatalogAlbum.serializer(), """{"token":"t","id":"9","title":"X","artist":"A","artist_id":null,"year":2001,"kind":"ep","cover":null,"tracks":[{"i":0,"title":"Y","year":"","no":3}]}""")
        assertEquals("2001", a.year)
        assertNull(a.artistId)
        assertNull(a.tracks[0].year)
        assertEquals(3, a.tracks[0].no)
    }

    @Test fun activityLyricsUploads() {
        val act = PiJson.decodeFromString(Activity.serializer(), """{"active":["A — T"],"jobs":[{"title":"X","total":3,"started":1}],"log":[{"ts":1,"text":"ok"}],"queue":2}""")
        assertEquals(2, act.queue)
        val l = PiJson.decodeFromString(Lyrics.serializer(), """{"synced":[{"t":12.34,"text":"a"},{"t":15,"text":"b"}],"plain":"a\nb","source":"lrc"}""")
        assertEquals(15.0, l.synced!![1].t, 0.0)
        val u = PiJson.decodeFromString(UploadResp.serializer(), """{"uploads":[],"rejected":[{"name":"a.txt","error":"use_import","message":"Списки — в импорт"}],"error":"use_import","message":"m"}""")
        assertEquals("use_import", u.rejected[0].error)
    }

    @Test fun settingsAndEqFormat() {
        val s = PiJson.decodeFromString(SettingsAll.serializer(), """
            {"items":{"eq.shared":{"value":{"active":"bass","preamp":-3,"bands":[{"type":"lowshelf","f":31,"g":6,"q":1}]},"updated_at":1760000000123},
                      "ui.theme":{"value":{"mode":"dark","accent":"ocean"},"updated_at":5}}}
        """)
        assertEquals(1760000000123L, s.items.getValue("eq.shared").updatedAt)
        val shared = PiJson.decodeFromJsonElement(EqController.JShared.serializer(), s.items.getValue("eq.shared").value!!)
        assertEquals("bass", shared.active)
        assertEquals("lowshelf", shared.bands!![0].type)
        // запись устройства — в формате eq.js (name, platform, mode, …)
        val dev = PiJson.encodeToJsonElement(EqController.JDevice.serializer(), EqController.JDevice(name = "Android · Pixel")).jsonObject
        assertEquals(JsonPrimitive("sync"), dev["mode"])
        assertEquals(JsonPrimitive("android"), dev["platform"])
        assertTrue(dev.containsKey("limiter"))
    }
}
