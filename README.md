# Bot de música privado para Discord

Toca música na chamada de voz a partir de **YouTube** (link, playlist ou busca por nome) e
**Spotify** (link de música, álbum ou playlist). Só você consegue usar os comandos.

Como o Spotify não libera o áudio, cada faixa do Spotify é procurada no YouTube pelo
"artista - nome" e tocada de lá.

## O que já está pronto nesta pasta

- `bot.py` – o bot.
- `.venv/` – Python com tudo instalado (discord.py, yt-dlp, spotipy...).
- `start.bat` – liga o bot. `update.bat` – atualiza o yt-dlp (faça isso quando o YouTube "quebrar").
- `.env` – configuração. **Falta só colar o token do Discord.**

Requisitos já atendidos nesta máquina: Python 3.13, FFmpeg no PATH e Node (usado pelo yt-dlp).

## Passo a passo (uma vez só)

### 1. Criar o bot no Discord
1. Abra <https://discord.com/developers/applications> e clique em **New Application**.
2. Na aba **Bot**:
   - clique em **Reset Token** e copie o token;
   - **desligue** "Public Bot" (só você poderá adicioná-lo a servidores);
   - não precisa ligar nenhuma "Privileged Gateway Intent".
3. Na aba **Installation**: em "Install Link" deixe **None** (ou "Discord Provided Link" só com Guild Install).

### 2. Colocar o bot no seu servidor
Na aba **OAuth2 → URL Generator**, marque os escopos `bot` e `applications.commands` e as
permissões *View Channels, Send Messages, Embed Links, Connect, Speak*. Ou use direto
(troque `SEU_CLIENT_ID` pelo "Application ID" da aba General Information):

```
https://discord.com/oauth2/authorize?client_id=SEU_CLIENT_ID&scope=bot%20applications.commands&permissions=3165184
```

### 3. Configurar e ligar
1. Abra o arquivo `.env` e cole o token em `DISCORD_TOKEN=`.
2. Dê dois cliques em `start.bat`.
3. No Discord, entre num canal de voz e digite `/play nome da música`.

Os comandos `/` aparecem em cada servidor assim que o bot entra (sincronização por servidor, sem espera).

## Comandos

| Comando | O que faz |
|---|---|
| `/play <busca ou link>` | Toca ou coloca na fila. Aceita link do YouTube (vídeo ou playlist), link do Spotify (música, álbum ou playlist) ou texto para buscar. |
| `/skip` | Pula a música atual. |
| `/pause` / `/resume` | Pausa e continua. |
| `/np` | Mostra o que está tocando, com barra de progresso. |
| `/queue` | Mostra a fila. |
| `/shuffle` | Embaralha a fila. |
| `/remove <posição>` | Tira uma música da fila. |
| `/clear` | Esvazia a fila (a música atual continua). |
| `/loop <desligar / música atual / fila>` | Repetição. |
| `/volume <0-200>` | Volume (100 = normal). |
| `/stop` | Para e esvazia a fila, mas fica no canal. |
| `/leave` | Sai do canal de voz. |

O bot sai sozinho do canal depois de 5 minutos sem nada para tocar ou sozinho na chamada
(`IDLE_TIMEOUT` no `.env`).

## Privacidade: quem pode usar

Por padrão, só o **dono do aplicativo** no Discord (você). Qualquer outra pessoa recebe
"🔒 Este bot é privado". Para liberar amigos, coloque os IDs deles em `ALLOWED_USER_IDS`
no `.env` (separados por vírgula). Para pegar o ID de alguém, ative "Modo desenvolvedor" nas
configurações do Discord e clique com o botão direito no usuário → "Copiar ID do usuário".

## Spotify

Já funciona sem configurar nada: o bot lê a página pública do link. A única limitação é que
playlists grandes vêm só com as primeiras ~50 a 100 faixas.

Para playlists completas, crie um app gratuito em <https://developer.spotify.com/dashboard>
(qualquer nome, Redirect URI pode ser `http://127.0.0.1:8888/callback`), copie o
**Client ID** e o **Client Secret** para `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` no `.env`
e reinicie o bot.

## Quando algo der errado

- **"Não consegui tocar... Sign in to confirm you're not a bot"** ou várias músicas falhando
  seguidas: o YouTube mudou algo. Feche o bot, rode `update.bat` e abra de novo. Se persistir,
  exporte os cookies do YouTube do seu navegador para um `cookies.txt` (extensão
  "Get cookies.txt LOCALLY") e aponte o caminho em `YTDL_COOKIES` no `.env`.
- **Comandos `/` não aparecem**: confira se o convite tinha o escopo `applications.commands`.
  Se precisar, tire o bot do servidor e convide de novo com o link acima.
- **O bot entra mas não sai som**: veja se ele não está silenciado no servidor
  (botão direito nele no canal de voz) e se o volume (`/volume`) não está em 0.
- **"FFmpeg não encontrado"**: `winget install Gyan.FFmpeg` e abra um novo terminal,
  ou coloque o caminho do `ffmpeg.exe` em `FFMPEG_PATH` no `.env`.

Detalhe técnico: o YouTube anda recusando (erro 403) os formatos de alguns "clientes" internos
sem PO Token. O bot testa cada URL antes de tocar, prefere o cliente que funciona
(`visionos` hoje) e, se nada abrir, tenta outras estratégias sozinho. Os logs na janela do
`start.bat` mostram qual formato/estratégia foi usada.

## Entra na call mas ninguém ouve nada (causa conhecida)

O Discord passou a exigir criptografia ponta a ponta de voz (protocolo DAVE); sem ela o servidor
de voz fecha a conexão com o código 4017. O discord.py 2.7.1 implementa isso usando a biblioteca
`davey`, mas a versão 0.2.0 da `davey` mudou a API: a sessão nunca fica "pronta", o discord.py
manda o áudio sem criptografia e os clientes não conseguem decodificar, ou seja, silêncio total,
como se o bot estivesse mudo.

Solução aplicada: `davey<0.2` fixado no `requirements.txt` (instalada a 0.1.7). Se um dia o
`update.bat` trouxer de volta a 0.2.x e o som sumir, rode na pasta do bot:

```
.venv\Scripts\python.exe -m pip install "davey<0.2"
```

Para diagnosticar, rode com `LOG_LEVEL=DEBUG` no `.env`: o `bot.log` passa a mostrar a cada 5 s
uma linha `voz: N pacotes enviados ... can_encrypt=True`. Se aparecer `can_encrypt=False`, é esse
problema de novo.
