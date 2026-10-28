# DTO kotlinx.serialization: правила поставляются с библиотекой; оставляем имена полей моделей API на всякий случай.
-keepclassmembers @kotlinx.serialization.Serializable class io.github.ikriu.pimusic.** {
    *** Companion;
    kotlinx.serialization.KSerializer serializer(...);
}
-keep class io.github.ikriu.pimusic.player.PlaybackService
-keep class io.github.ikriu.pimusic.player.DownloadsService
-dontwarn org.conscrypt.**
-dontwarn org.bouncycastle.**
-dontwarn org.openjsse.**
