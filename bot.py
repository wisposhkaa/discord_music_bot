import discord
from discord.ext import commands
import yt_dlp
import asyncio
import random
import time
import urllib.parse
import math
import os
import json
from dotenv import load_dotenv

load_dotenv()

# --- 1. НАСТРОЙКИ БОТА ---
intents = discord.Intents.default()
intents.message_content = True
# Отключаем стандартный help, чтобы использовать наш красивый
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# Словари для хранения данных
queues = {} 
settings = {} 
current_tracks = {} 
playback_info = {} 
is_seeking = {}
is_processing = {}
now_playing_messages = {}
history_queues = {}
loop_mode = {}

SETTINGS_FILE = "server_settings.json"

# Функция для загрузки настроек из файла
def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r") as f:
            return json.load(f)
    return {}

# Функция для сохранения настроек в файл
def save_settings(data):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=4)

# Загружаем настройки при старте бота
persistent_settings = load_settings()

PLAYLIST_HISTORY_FILE = "playlist_history.json"

# Функция для загрузки истории плейлистов из файла
def load_playlists():
    if os.path.exists(PLAYLIST_HISTORY_FILE):
        with open(PLAYLIST_HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

# Функция для сохранения истории в файл
def save_playlists(data):
    with open(PLAYLIST_HISTORY_FILE, "w", encoding="utf-8") as f:
        # ensure_ascii=False нужен, чтобы русские буквы сохранялись нормально
        json.dump(data, f, indent=4, ensure_ascii=False)

# Теперь вместо пустого словаря {} мы сразу загружаем данные из файла
saved_playlists = load_playlists()

YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'ignoreerrors': True, 
}
ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)

# Улучшенные настройки для идеального звука без заиканий
FFMPEG_OPTIONS = {
    # -analyzeduration 0 и -probesize 32k запрещают скачивать в память большие куски для анализа
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -analyzeduration 0 -probesize 32k',
    # -threads 1 заставляет FFmpeg использовать минимум памяти и процессора
    'options': '-vn -threads 1' 
}

class QueueView(discord.ui.View):
    def __init__(self, queue_list, playing_now, ctx):
        super().__init__(timeout=60)
        self.queue_list = queue_list
        self.playing_now = playing_now
        self.ctx = ctx
        self.current_page = 0
        self.per_page = 10
        # Защита от деления на ноль, если очередь пуста
        self.total_pages = math.ceil(len(queue_list) / self.per_page) if len(queue_list) > 0 else 1

    def create_embed(self):
        """Создает эмбед для текущей страницы."""
        embed = discord.Embed(title="📋 Очередь треков", color=discord.Color.blue())
        
        if self.playing_now:
            embed.add_field(name="🔊 Сейчас играет:", value=self.playing_now['title'], inline=False)

        if not self.queue_list:
            embed.description = "Очередь пуста."
            return embed

        # Вычисляем индексы треков для текущей страницы
        start = self.current_page * self.per_page
        end = start + self.per_page
        current_list = self.queue_list[start:end]

        queue_text = ""
        for i, t in enumerate(current_list, start + 1):
            queue_text += f"**{i}.** {t['title']}\n"

        embed.add_field(name=f"⏳ Ожидают (стр. {self.current_page + 1}/{self.total_pages}):", value=queue_text, inline=False)
        embed.set_footer(text=f"Всего треков в очереди: {len(self.queue_list)}")
        return embed

    @discord.ui.button(label="⬅️ Назад", style=discord.ButtonStyle.gray)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.ctx.author:
            return await interaction.response.send_message("Это не ваша очередь!", ephemeral=True)
        
        if self.current_page > 0:
            self.current_page -= 1
            await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="Вперед ➡️", style=discord.ButtonStyle.gray)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.ctx.author:
            return await interaction.response.send_message("Это не ваша очередь!", ephemeral=True)

        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            await interaction.response.edit_message(embed=self.create_embed(), view=self)
        
async def fetch_missing_titles(tracks):
    """Фоновая задача: подгружает настоящие названия ВСЕХ треков без остановки бота"""
    loop = asyncio.get_running_loop()
    with yt_dlp.YoutubeDL({'quiet': True, 'noplaylist': True}) as ydl:
        for track in tracks:
            # Проверяем только те, которые еще не загрузились
            if track.get('title') == "⌛ Ожидает загрузки...":
                try:
                    info = await loop.run_in_executor(None, lambda: ydl.extract_info(track['url'], download=False))
                    if info:
                        title = info.get('title')
                        if not title or title.isdigit():
                            title = f"{info.get('uploader', 'SoundCloud')} - {info.get('track', 'Трек')}"
                        track['title'] = title 
                    
                    # МАЛЕНЬКАЯ ПАУЗА: чтобы SoundCloud не забанил нас за спам
                    await asyncio.sleep(0.3) 
                except Exception:
                    pass
class PlaybackView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=None)
        self.ctx = ctx

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.gray)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = self.ctx.guild.id
        if guild_id not in history_queues or not history_queues[guild_id]:
            return await interaction.response.send_message("История пуста!", ephemeral=True)

        prev_track = history_queues[guild_id].pop()
        current = current_tracks.get(guild_id)
        if current:
            queues[guild_id].insert(0, current)
        
        queues[guild_id].insert(0, prev_track)
        
        await interaction.response.defer()
        self.ctx.voice_client.stop()

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.blurple)
    async def play_pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.ctx.voice_client: return
            
        if self.ctx.voice_client.is_playing():
            self.ctx.voice_client.pause()
            await interaction.response.send_message("⏸️ Пауза", ephemeral=True)
        elif self.ctx.voice_client.is_paused():
            self.ctx.voice_client.resume()
            await interaction.response.send_message("▶️ Продолжаем", ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.gray)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.ctx.voice_client and (self.ctx.voice_client.is_playing() or self.ctx.voice_client.is_paused()):
            await interaction.response.defer()
            self.ctx.voice_client.stop()
        else:
            await interaction.response.send_message("Ничего не играет.", ephemeral=True)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.gray)
    async def shuffle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = self.ctx.guild.id
        if guild_id in queues and len(queues[guild_id]) > 1:
            random.shuffle(queues[guild_id])
            await interaction.response.send_message("🔀 Очередь перемешана!", ephemeral=True)
        else:
            await interaction.response.send_message("Недостаточно треков для перемешивания.", ephemeral=True)

    @discord.ui.button(emoji="📋", style=discord.ButtonStyle.gray)
    async def queue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = self.ctx.guild.id
        playing_now = current_tracks.get(guild_id)
        queue_list = queues.get(guild_id, [])

        if not playing_now and not queue_list:
            return await interaction.response.send_message("Очередь пуста.", ephemeral=True)

        view = QueueView(queue_list, playing_now, self.ctx)
        embed = view.create_embed()
        
        # Если страниц больше одной — прикрепляем кнопки перелистывания
        if view.total_pages > 1:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        # Если страница всего одна — отправляем просто текст, вообще не упоминая view
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

# --- 2. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def get_server_settings(guild_id):
    if guild_id not in settings:
        settings[guild_id] = {'shuffle': False, 'repeat': False}
    return settings[guild_id]
# Упрощенные настройки для стабильности на Windows
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn' # Убрали сложные настройки битрейта для теста
}

async def play_next(ctx, error=None):
    if error: print(f"Ошибка FFmpeg: {error}")
    
    guild_id = ctx.guild.id
    
    # 1. Сбрасываем замок обработки, чтобы позволить новый запуск
    is_processing[guild_id] = False

    if not ctx.voice_client or not ctx.voice_client.is_connected():
        return

    if ctx.voice_client.is_playing() and not is_seeking.get(guild_id):
        return

    # 2. Достаем трек из очереди
    if is_seeking.get(guild_id):
        track = current_tracks.get(guild_id)
        seek_offset = playback_info[guild_id]['seek_offset']
        is_seeking[guild_id] = False 
    else:
        # 1. Вытаскиваем старый трек и сохраняем его в историю
        old_track = current_tracks.get(guild_id)
        if old_track:
            if guild_id not in history_queues: history_queues[guild_id] = []
            history_queues[guild_id].append(old_track)
            if len(history_queues[guild_id]) > 50: history_queues[guild_id].pop(0)
            
            # ---> МАГИЯ: Возвращаем трек в конец очереди (если включен цикл)
            # По умолчанию мы считаем, что цикл включен (True)
            if loop_mode.get(guild_id, True): 
                if guild_id not in queues: queues[guild_id] = []
                queues[guild_id].append(old_track)

        # 2. Берем следующий трек на воспроизведение
        if guild_id in queues and len(queues[guild_id]) > 0:
            track = queues[guild_id].pop(0)
            current_tracks[guild_id] = track
            seek_offset = 0 
        else:
            if guild_id in current_tracks: del current_tracks[guild_id]
            return

    is_processing[guild_id] = True

    try:
        loop = asyncio.get_running_loop()
        
        # 3. Извлекаем прямую ссылку. 
        with yt_dlp.YoutubeDL({**YTDL_OPTIONS, 'noplaylist': True}) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(track['url'], download=False))
            real_url = info['url']
            
            real_title = info.get('title')
            if not real_title or real_title.isdigit():
                uploader = info.get('uploader', 'SoundCloud')
                track_name = info.get('track', 'Трек')
                real_title = f"{uploader} - {track_name}"

            # Обновляем инфо в словаре
            current_tracks[guild_id]['title'] = real_title
            current_tracks[guild_id]['duration'] = info.get('duration', 0) # <--- СОХРАНЯЕМ ДЛИНУ ПЕСНИ
            title = real_title 

        playback_info[guild_id] = {'start_time': time.time(), 'seek_offset': seek_offset}
        
        ffmpeg_params = dict(FFMPEG_OPTIONS)
        if seek_offset > 0:
            # <--- ФИКС FFmpeg: ставим -ss целым числом В САМОЕ НАЧАЛО настроек!
            # Это решает проблему зависания и ошибок декодирования
            ffmpeg_params['before_options'] = f"-ss {int(seek_offset)} {FFMPEG_OPTIONS['before_options']}"
            
        # Базовый источник звука
        base_source = discord.FFmpegPCMAudio(real_url, executable="ffmpeg", **ffmpeg_params)
        
        # Достаем сохраненную громкость (по умолчанию 1.0, то есть 100%)
        # JSON хранит ключи как строки, поэтому переводим guild_id в строку
        guild_str = str(guild_id)
        current_vol = persistent_settings.get(guild_str, {}).get("volume", 1.0)
        
        # Оборачиваем звук в трансформатор громкости
        source = discord.PCMVolumeTransformer(base_source, volume=current_vol)
        
        def after_playing(e):
            is_processing[guild_id] = False
            asyncio.run_coroutine_threadsafe(play_next(ctx, e), bot.loop)

        ctx.voice_client.play(source, after=after_playing)
        is_processing[guild_id] = False
        
        # 4. Отправляем или обновляем карточку
        if not is_seeking.get(guild_id):
            embed = discord.Embed(
                description=f"🎶 **Сейчас играет:**\n**{title}**", 
                color=discord.Color.green()
            )
            
            # Создаем нашу новую панель с 5 кнопками
            view = PlaybackView(ctx) 
            
            old_message = now_playing_messages.get(guild_id)
            if old_message:
                try:
                    await old_message.edit(embed=embed, view=view)
                except:
                    now_playing_messages[guild_id] = await ctx.send(embed=embed, view=view)
            else:
                now_playing_messages[guild_id] = await ctx.send(embed=embed, view=view)
            
    except Exception as e:
        print(f"Ошибка при попытке играть: {e}")
        is_processing[guild_id] = False
        # В случае ошибки ждем 2 секунды и идем к следующему треку
        await asyncio.sleep(2)
        await play_next(ctx)

async def seek_music(ctx, delta_seconds: int):
    guild_id = ctx.guild.id
    if not ctx.voice_client or not ctx.voice_client.is_playing():
        await ctx.send(embed=discord.Embed(description="❌ Сейчас ничего не играет.", color=discord.Color.red()))
        return
        
    if guild_id not in current_tracks or guild_id not in playback_info: return
        
    elapsed = time.time() - playback_info[guild_id]['start_time']
    current_position = playback_info[guild_id]['seek_offset'] + elapsed
    
    # 1. Защита от ухода в минус
    new_position = max(0, current_position + delta_seconds)
    
    # 2. Если пытаемся перемотать дальше конца песни - просто переключаем на следующую
    duration = current_tracks[guild_id].get('duration', 0)
    if duration and new_position >= duration - 2:
        is_seeking[guild_id] = False # Отменяем статус перемотки
        ctx.voice_client.stop() # Остановка вызовет play_next автоматически
        return

    playback_info[guild_id]['seek_offset'] = new_position
    is_seeking[guild_id] = True
    ctx.voice_client.stop()

# --- 3. КОМАНДЫ БОТА ---
@bot.event
async def on_ready():
    print(f'✅ Бот {bot.user.name} успешно запущен!')

@bot.command()
async def play(ctx, *, query: str):
    if not ctx.message.author.voice:
        await ctx.send(embed=discord.Embed(description="❌ Тебе нужно зайти в голосовой канал!", color=discord.Color.red()))
        return

    voice_channel = ctx.message.author.voice.channel
    if not ctx.voice_client:
        await voice_channel.connect()

    # Желтая карточка поиска
    search_embed = discord.Embed(description=f"🔍 Ищу трек: `{query}`...", color=discord.Color.gold())
    message = await ctx.send(embed=search_embed)

    # Если это не прямая ссылка, ищем в SoundCloud
    if not query.startswith('http'):
        query = f"scsearch:{query}"

    try:
        loop = asyncio.get_event_loop()
        # Извлекаем информацию о треке
        with yt_dlp.YoutubeDL({**YTDL_OPTIONS, 'noplaylist': True}) as ydl:
            data = await loop.run_in_executor(None, lambda: ydl.extract_info(query, download=False))
        
        if 'entries' in data:
            data = data['entries'][0]
            
        track_info = {
            'url': data['webpage_url'], # Используем webpage_url для повторной экстракции в play_next
            'title': data.get('title', 'Неизвестный трек')
        }
        
        guild_id = ctx.guild.id
        if guild_id not in queues: 
            queues[guild_id] = []
        
        # Добавляем трек в список очереди
        queues[guild_id].append(track_info)

        # Сообщаем об успехе
        success_embed = discord.Embed(
            description=f"✅ **Добавлено в очередь:**\n{track_info['title']}", 
            color=discord.Color.green()
        )
        await message.edit(embed=success_embed)

        # Если сейчас ничего не играет и бот не занят обработкой — запускаем!
        if not ctx.voice_client.is_playing() and not is_processing.get(guild_id, False):
            await play_next(ctx)

    except Exception as e:
        error_embed = discord.Embed(description="❌ Не удалось найти этот трек или произошла ошибка.", color=discord.Color.red())
        await message.edit(embed=error_embed)
        print(f"Ошибка yt-dlp: {e}")

@bot.command(aliases=['pl'])
async def playlist(ctx, *, query: str):
    original_query = query
    
    if query.lower().strip() == "noize mc":
        query = "https://soundcloud.com/katerina-kapustina-533494326/sets/noize-mc"

    if not query.startswith(("http://", "https://")):
        return await ctx.send(embed=discord.Embed(description="❌ Отправь ссылку на плейлист!\n*(Или используй: `!playlist noize mc`)*", color=discord.Color.red()))

    if not ctx.message.author.voice:
        return await ctx.send(embed=discord.Embed(description="❌ Зайди в голосовой канал!", color=discord.Color.red()))

    if not ctx.voice_client:
        await ctx.message.author.voice.channel.connect()

    loading_embed = discord.Embed(description="⏳ Читаю плейлист... Это может занять пару секунд.", color=discord.Color.orange())
    message = await ctx.send(embed=loading_embed)

    YTDL_OPTS = {
        'extract_flat': True,
        'noplaylist': False,
        'quiet': True,
    }

    try:
        loop = asyncio.get_event_loop()
        with yt_dlp.YoutubeDL(YTDL_OPTS) as ydl:
            data = await loop.run_in_executor(None, lambda: ydl.extract_info(query, download=False))

        if not data or 'entries' not in data:
            return await message.edit(embed=discord.Embed(description="❌ По этой ссылке не найден плейлист.", color=discord.Color.red()))

        guild_id = ctx.guild.id
        if guild_id not in queues: 
            queues[guild_id] = []

        added_count = 0
        for entry in data['entries']:
            if not entry: continue
            
            title = entry.get('title')
            if not title or title.isdigit():
                title = "⌛ Ожидает загрузки..."
                
            url = entry.get('url') or entry.get('webpage_url')
            
            if url:
                queues[guild_id].append({
                    'url': url,
                    'title': title
                })
                added_count += 1

        if added_count == 0:
            return await message.edit(embed=discord.Embed(description="❌ Плейлист оказался пустым.", color=discord.Color.red()))

        playlist_title = data.get('title', 'Без названия')

        # --- НАЧАЛО ЗАМЕНЫ ---
        # JSON хранит ключи как строки, поэтому переводим guild_id в строку
        guild_str = str(ctx.guild.id)
        
        if guild_str not in saved_playlists:
            saved_playlists[guild_str] = []
            
        saved_playlists[guild_str].append({
            'title': playlist_title,
            'url': query,
            'query': original_query 
        })
        
        # Оставляем только 10 последних
        if len(saved_playlists[guild_str]) > 10:
            saved_playlists[guild_str].pop(0) 

        # Сразу сохраняем обновленный список в файл!
        save_playlists(saved_playlists)
        # --- КОНЕЦ ЗАМЕНЫ ---

        # ---> МАГИЯ: Запускаем фоновую подгрузку... (и дальше как было)

        # ---> МАГИЯ: Запускаем фоновую подгрузку для первых 15 треков <---
        # Берем только те треки, которые мы только что добавили
        new_tracks = queues[guild_id][-added_count:]
        bot.loop.create_task(fetch_missing_titles(new_tracks))

        await message.edit(embed=discord.Embed(
            description=f"✅ Добавлено **{added_count}** треков из плейлиста: **{playlist_title}**",
            color=discord.Color.green()
        ))

        if not ctx.voice_client.is_playing() and not is_processing.get(guild_id):
            await play_next(ctx)

    except Exception as e:
        print(f"Ошибка плейлиста: {e}")
        await message.edit(embed=discord.Embed(description="❌ Ошибка при чтении плейлиста.", color=discord.Color.red()))

@bot.command(aliases=['pl_history', 'history'])
async def playlist_history(ctx):
    guild_id = ctx.guild.id
    history = saved_playlists.get(guild_id, [])
    
    if not history:
        return await ctx.send(embed=discord.Embed(description="📭 История плейлистов пока пуста.", color=discord.Color.orange()))
        
    description = ""
    # Разворачиваем список задом наперед, чтобы самые свежие были сверху
    for i, item in enumerate(reversed(history), 1):
        # Если юзали шорткат, покажем его в скобках
        query_text = f" *(запрос: {item['query']})*" if item['query'].lower() == "noize mc" else ""
        description += f"**{i}.** [{item['title']}]({item['url']}){query_text}\n"
        
    embed = discord.Embed(
        title="📜 Последние загруженные плейлисты", 
        description=description, 
        color=discord.Color.blurple()
    )
    embed.set_footer(text="Отображаются последние 10 плейлистов")
    await ctx.send(embed=embed)
        
@bot.command()
async def clear(ctx):
    """Очищает очередь, если ты случайно загрузил слишком длинный плейлист."""
    guild_id = ctx.guild.id
    if guild_id in queues:
        queues[guild_id] = []
        await ctx.send(embed=discord.Embed(description="🗑️ **Очередь полностью очищена!**", color=discord.Color.blue()))
    else:
        await ctx.send(embed=discord.Embed(description="Очередь и так пуста.", color=discord.Color.orange()))

@bot.command(aliases=['skip'])
async def next(ctx, count: int = 1):
    """Пропускает текущий трек или сразу несколько (например: !skip 5)"""
    if count < 1:
        return await ctx.send(embed=discord.Embed(description="❌ Число должно быть 1 или больше!", color=discord.Color.red()))

    guild_id = ctx.guild.id

    # Проверяем, играет ли что-то прямо сейчас (или стоит на паузе)
    if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
        
        # Если просят пропустить больше 1 трека, убираем их из очереди
        if count > 1 and guild_id in queues:
            # Считаем, сколько треков удалить из начала очереди
            # Вычитаем 1, так как текущий играющий трек мы пропустим просто остановив плеер
            to_skip = min(count - 1, len(queues[guild_id]))
            
            for _ in range(to_skip):
                skipped_track = queues[guild_id].pop(0)
                
                # Сохраняем пропущенные треки в историю
                if guild_id not in history_queues: 
                    history_queues[guild_id] = []
                history_queues[guild_id].append(skipped_track)
                if len(history_queues[guild_id]) > 50: 
                    history_queues[guild_id].pop(0)
                
                # Если включен повтор очереди (loop_mode), отправляем их в конец списка
                if loop_mode.get(guild_id, True):
                    queues[guild_id].append(skipped_track)

        # Останавливаем текущий трек. Это автоматически вызовет функцию play_next 
        # и бот начнет играть уже нужный трек
        ctx.voice_client.stop()
        
        # Выбираем правильный текст для сообщения
        text = "⏭️ **Трек пропущен!**" if count == 1 else f"⏭️ **Пропущено треков: {count}**"
        await ctx.send(embed=discord.Embed(description=text, color=discord.Color.blue()))
        
    else:
        await ctx.send(embed=discord.Embed(description="В данный момент ничего не играет.", color=discord.Color.orange()))
@bot.command(aliases=['queue', 'q'])
async def query(ctx):
    guild_id = ctx.guild.id
    playing_now = current_tracks.get(guild_id)
    queue_list = queues.get(guild_id, [])

    # Если совсем ничего нет
    if not playing_now and not queue_list:
        embed = discord.Embed(
            description="📭 Очередь пуста и музыка не играет.", 
            color=discord.Color.orange()
        )
        return await ctx.send(embed=embed)

    # Создаем наше интерактивное меню
    view = QueueView(queue_list, playing_now, ctx)
    
    # Если страниц больше одной, показываем кнопки. Если одна — кнопки не нужны.
    if view.total_pages <= 1:
        await ctx.send(embed=view.create_embed())
    else:
        await ctx.send(embed=view.create_embed(), view=view)

@bot.command()
async def stop(ctx):
    guild_id = ctx.guild.id
    if guild_id in queues: queues[guild_id] = []
    if guild_id in current_tracks: del current_tracks[guild_id]
    if guild_id in playback_info: del playback_info[guild_id] 
    
    # ---> УДАЛЯЕМ СООБЩЕНИЕ <---
    if guild_id in now_playing_messages:
        try:
            await now_playing_messages[guild_id].delete()
        except:
            pass
        del now_playing_messages[guild_id]
        
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send(embed=discord.Embed(description="⏹️ **Музыка остановлена. Очередь очищена.**", color=discord.Color.red()))

@bot.command()
async def shuffle(ctx):
    guild_id = ctx.guild.id
    if guild_id in queues and len(queues[guild_id]) > 1:
        random.shuffle(queues[guild_id])
        await ctx.send(embed=discord.Embed(description="🔀 **Очередь перемешана!**", color=discord.Color.purple()))
    else:
        await ctx.send(embed=discord.Embed(description="В очереди недостаточно треков.", color=discord.Color.orange()))

@bot.command(aliases=['ff', 'fwd'])
async def forward(ctx, seconds: int):
    await ctx.send(embed=discord.Embed(description=f"⏩ **Перематываю вперед на {seconds} сек...**", color=discord.Color.blue()))
    await seek_music(ctx, seconds)

@bot.command(aliases=['rw', 'back'])
async def backwards(ctx, seconds: int):
    await ctx.send(embed=discord.Embed(description=f"⏪ **Перематываю назад на {seconds} сек...**", color=discord.Color.blue()))
    await seek_music(ctx, -seconds)

@bot.command()
async def help(ctx):
    embed = discord.Embed(
        title="🎵 Музыкальный Бот | Список команд",
        description="Я умею находить и включать музыку с отличным качеством!",
        color=discord.Color.blurple() 
    )
    embed.add_field(name="▶️ Основные", value="`!play <название>` — Включить трек\n`!stop` — Остановить и выгнать бота", inline=False)
    embed.add_field(name="📋 Очередь", value="`!queue` — Показать список\n`!next` — Следующая песня\n`!shuffle` — Перемешать", inline=False)
    embed.add_field(name="⏳ Перемотка", value="`!forward <сек>` (или `!ff`) — Вперед\n`!backwards <сек>` (или `!rw`) — Назад", inline=False)
    embed.add_field(name="📜 Плейлисты", value="`!playlist <ссылка>` — Добавить весь плейлист из SoundCloud", inline=False)
    embed.add_field(name="🎤 Авторы", value="`!author \"имя\" <кол-во>` — Захватить топ треков автора", inline=False)
    embed.set_footer(text="Приятного прослушивания! 🎧")
    await ctx.send(embed=embed)

@bot.command(aliases=['artist', 'author'])
async def play_author(ctx, *, query: str):
    if not ctx.message.author.voice:
        return await ctx.send(embed=discord.Embed(description="❌ Зайди в голосовой канал!", color=discord.Color.red()))

    if not ctx.voice_client:
        await ctx.message.author.voice.channel.connect()

    # --- УМНЫЙ РАЗБОР ТЕКСТА ---
    parts = query.strip().split()
    # Проверяем, является ли последнее слово числом
    if len(parts) > 1 and parts[-1].isdigit():
        count = int(parts[-1])
        name = " ".join(parts[:-1]) # Всё, кроме последнего слова, идет в имя
    else:
        count = 60 # По умолчанию качаем 60, как в старой версии
        name = query # Всё введенное - это имя автора

    if count > 100:
        count = 100
        await ctx.send("⚠️ Максимум 100 треков за раз.", delete_after=5)

    message = await ctx.send(embed=discord.Embed(
        description=f"🤖 Ищу: **{name}**\nЦель: **{count}** треков...", 
        color=discord.Color.orange()
    ))

    try:
        loop = asyncio.get_event_loop()
        # Ищем ровно {count} треков по имени {name}
        search_query = f"scsearch{count}:{name}"
        
        YTDL_SEARCH_OPTS = {'extract_flat': True, 'quiet': True, 'force_generic_extractor': False}

        with yt_dlp.YoutubeDL(YTDL_SEARCH_OPTS) as ydl:
            data = await loop.run_in_executor(None, lambda: ydl.extract_info(search_query, download=False))

        if not data or 'entries' not in data or len(data['entries']) == 0:
            return await message.edit(embed=discord.Embed(description=f"❌ Ничего не найдено по запросу: {name}", color=discord.Color.red()))

        guild_id = ctx.guild.id
        if guild_id not in queues: queues[guild_id] = []

        added_count = 0
        new_tracks = []
        for entry in data['entries']:
            if not entry: continue
            t_url = entry.get('url') or entry.get('webpage_url')
            if t_url:
                track_data = {'url': t_url, 'title': entry.get('title', 'Трек SoundCloud')}
                queues[guild_id].append(track_data)
                new_tracks.append(track_data)
                added_count += 1

        bot.loop.create_task(fetch_missing_titles(new_tracks))

        await message.edit(embed=discord.Embed(
            description=f"🔥 **{name}** захвачен!\nДобавлено в очередь: **{added_count}** треков.", 
            color=discord.Color.green()
        ))

        if not ctx.voice_client.is_playing() and not is_processing.get(guild_id):
            await play_next(ctx)

    except Exception as e:
        print(f"Ошибка AUTHOR: {e}")
        await message.edit(embed=discord.Embed(description="❌ Произошел сбой.", color=discord.Color.red()))
    
@bot.command(aliases=['repeat'])
async def loop(ctx):
    """Включает или выключает бесконечный повтор очереди."""
    guild_id = ctx.guild.id
    
    # Меняем текущее значение на противоположное (по умолчанию включено)
    current_loop = loop_mode.get(guild_id, True)
    loop_mode[guild_id] = not current_loop
    
    state = "✅ **Включен**" if loop_mode[guild_id] else "❌ **Выключен**"
    await ctx.send(embed=discord.Embed(description=f"🔁 Бесконечный повтор очереди: {state}", color=discord.Color.blue()))
    
@bot.command(aliases=['vol'])
async def volume(ctx, vol: int):
    """Изменяет громкость бота (от 0 до 200%)."""
    if vol < 0 or vol > 200:
        return await ctx.send(embed=discord.Embed(description="❌ Громкость должна быть от 0 до 200!", color=discord.Color.red()))

    guild_str = str(ctx.guild.id)
    
    # Если сервера еще нет в настройках - создаем
    if guild_str not in persistent_settings:
        persistent_settings[guild_str] = {}

    # Дискорд принимает громкость от 0.0 до 2.0 (где 1.0 - это 100%)
    volume_float = vol / 100.0
    
    # Сохраняем в наш словарь и сразу записываем в файл
    persistent_settings[guild_str]["volume"] = volume_float
    save_settings(persistent_settings)

    # Если бот прямо сейчас что-то играет, меняем громкость на лету!
    if ctx.voice_client and ctx.voice_client.source:
        ctx.voice_client.source.volume = volume_float

    await ctx.send(embed=discord.Embed(description=f"🔊 **Громкость установлена на {vol}%**", color=discord.Color.blue()))

# --- ЗАПУСК ---
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
bot.run(DISCORD_TOKEN)