"""MCP server definition: tools for controlling a Soundcraft Ui mixer."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .config import Settings
from .controller import MixerController
from .mixer import MixerClient, MixerNotReady
from .paths import AddressError
from .safety import SafetyError

INSTRUCTIONS = """\
Soundcraft Ui シリーズ (Ui12) のデジタルミキサーを操作する MCP サーバです。DJ イベントの本番中に使われます。
- まず get_mixer_overview で現状を把握してから操作してください。
- 音量を上げる操作 (フェーダー/AUX センドを上げる、ミュート解除、EQ ブースト) はサーバ側で自動的に
  ゆっくりフェードします。ツールは完了を待たずに返るので、進行状況は mixer_status で確認できます。
- 下げる操作とミュートは即時です。問題が起きたら mute か stop_fades を使ってください。
- get_meters で信号の有無やレベル、get_graphic_eq / set_graphic_eq_band で AUX・マスターの
  グラフィック EQ (ハウリング対策など) を扱えます。
- 安全上限を超える操作は拒否されます。ユーザーが明示的に望んだ場合のみ confirm=true を付けてください。
"""

ChannelTypeArg = Annotated[
    Literal["master", "input", "line", "player", "fx", "sub", "aux"],
    Field(description="チャンネル種別。aux はマスターバス上の AUX マスターフェーダー"),
]
ChannelNumberArg = Annotated[int | None, Field(description="チャンネル番号 (1始まり)。master では省略", ge=1)]
SourceTypeArg = Annotated[Literal["input", "line", "player", "fx"], Field(description="AUX に送るソースの種別")]
BusArg = Annotated[int, Field(description="AUX バス番号 (Ui12 は 1〜4)", ge=1)]
FadeArg = Annotated[
    int | None,
    Field(
        description="フェード時間 (ms)。下げる時に指定するとその時間で下げる。上げる時は安全速度より速くはならない",
        ge=0,
    ),
]
ConfirmArg = Annotated[bool, Field(description="安全上限を超える操作を明示的に許可する。ユーザーが望んだ時だけ true")]

EqTypeArg = Annotated[Literal["input", "line", "player", "fx", "sub"], Field(description="EQ を持つチャンネル種別")]
DynTypeArg = Annotated[Literal["input", "line", "player", "fx", "sub", "aux"], Field(description="チャンネル種別")]
Norm = Annotated[float | None, Field(ge=0, le=1, description="正規化値 0〜1")]

GeqTargetArg = Annotated[Literal["master", "aux"], Field(description="グラフィック EQ の対象")]
GeqAuxArg = Annotated[int | None, Field(description="target=aux の時の AUX 番号 (1始まり)", ge=1)]

READ = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)


def build_server(settings: Settings, mixer: MixerClient | None = None) -> MCPServer:
    mixer = mixer or MixerClient(settings.mixer_ip, tick_sec=settings.ramp_tick_sec)
    ctl = MixerController(mixer, settings)

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[dict[str, Any]]:
        await mixer.start()  # idempotent; one shared connection for every session
        yield {}

    mcp = MCPServer("soundcraft-ui", instructions=INSTRUCTIONS, lifespan=lifespan)

    async def run(fn: Callable[[], Any], *, need_ready: bool = True) -> Any:
        try:
            if need_ready:
                await mixer.require_ready()
            result = fn()
            if isinstance(result, Awaitable):
                result = await result
            return result
        except (AddressError, SafetyError, MixerNotReady, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    # --- status ---------------------------------------------------------------

    @mcp.tool(annotations=READ)
    async def mixer_status() -> dict[str, Any]:
        """接続状態、機種、ファームウェア、進行中のフェード、安全設定を返す。"""
        await mixer.start()
        await mixer.wait_ready(3)
        return ctl.status()

    @mcp.tool(annotations=WRITE)
    async def mixer_reconnect() -> dict[str, Any]:
        """ミキサーへの WebSocket 接続を張り直す。"""
        await mixer.reconnect()
        await mixer.wait_ready(5)
        return ctl.status()

    @mcp.tool(annotations=WRITE)
    async def stop_fades() -> dict[str, Any]:
        """進行中のフェード (ゆっくり上げ/下げ) を全て止め、その時点の値で固定する。"""
        return await run(ctl.stop_fades, need_ready=False)

    # --- overview -----------------------------------------------------------

    @mcp.tool(annotations=READ)
    async def get_mixer_overview() -> dict[str, Any]:
        """マスターと全チャンネル (input/line/player/fx/sub/aux) の名前・フェーダー dB・
        ミュート・ソロ・パン (-100=L〜+100=R)・ステレオリンクを一覧で返す。"""
        return await run(ctl.overview)

    @mcp.tool(annotations=READ)
    async def get_channel(channel_type: ChannelTypeArg, channel: ChannelNumberArg = None) -> dict[str, Any]:
        """1チャンネルの詳細 (フェーダー、ミュート、各 AUX へのセンド、EQ、ダイナミクス) を返す。"""
        return await run(lambda: ctl.channel_detail(ctl.channel(channel_type, channel)))

    # --- mixing -------------------------------------------------------------

    @mcp.tool(annotations=WRITE)
    async def set_fader(
        channel_type: ChannelTypeArg,
        channel: ChannelNumberArg = None,
        db: Annotated[float | None, Field(description="目標レベル (dB, 最大 +10)。-100 以下で -inf")] = None,
        delta_db: Annotated[float | None, Field(description="現在値からの相対変化 (dB)")] = None,
        fade_ms: FadeArg = None,
        confirm: ConfirmArg = False,
    ) -> dict[str, Any]:
        """フェーダーを設定する (master / チャンネル / AUX マスター)。db か delta_db のどちらかを指定。
        上げる場合は自動でゆっくりフェードし、完了を待たずに返る。下げる場合は即時。"""

        def op() -> dict[str, Any]:
            target = db if db is None or db > -100 else float("-inf")
            return ctl.set_fader(ctl.channel(channel_type, channel), target, delta_db, fade_ms, confirm)

        return await run(op)

    @mcp.tool(annotations=WRITE)
    async def set_mute(
        channel_type: Annotated[
            Literal["input", "line", "player", "fx", "sub", "aux"], Field(description="チャンネル種別")
        ],
        channel: Annotated[int, Field(description="チャンネル番号 (1始まり)", ge=1)],
        muted: Annotated[bool, Field(description="true でミュート、false で解除")],
    ) -> dict[str, Any]:
        """チャンネルをミュート/解除する。ミュートは即時。解除は一旦フェーダーを絞ってから
        元のレベルまでゆっくり戻す (急に音が出ないように)。"""
        return await run(lambda: ctl.set_mute(ctl.channel(channel_type, channel), muted))

    @mcp.tool(annotations=WRITE)
    async def set_solo(
        channel_type: Annotated[
            Literal["input", "line", "player", "fx", "sub", "aux"], Field(description="チャンネル種別")
        ],
        channel: Annotated[int, Field(description="チャンネル番号 (1始まり)", ge=1)],
        solo: bool,
    ) -> dict[str, Any]:
        """チャンネルのソロ (ヘッドホンモニター) を切り替える。"""
        return await run(lambda: ctl.set_solo(ctl.channel(channel_type, channel), solo))

    @mcp.tool(annotations=WRITE)
    async def set_pan(
        channel_type: ChannelTypeArg,
        channel: ChannelNumberArg = None,
        pan: Annotated[float, Field(description="-100 (左) 〜 0 (中央) 〜 +100 (右)", ge=-100, le=100)] = 0,
    ) -> dict[str, Any]:
        """パンを設定する。"""
        return await run(lambda: ctl.set_pan(ctl.channel(channel_type, channel), pan))

    # --- AUX ----------------------------------------------------------------

    @mcp.tool(annotations=READ)
    async def get_aux_mix(bus: BusArg) -> dict[str, Any]:
        """AUX バス (モニター返し等) の内容: AUX マスターと、各ソースからのセンド量・ミュート・PRE/POST。"""
        return await run(lambda: ctl.aux_mix(bus))

    @mcp.tool(annotations=WRITE)
    async def set_aux_send(
        bus: BusArg,
        source_type: SourceTypeArg,
        source: Annotated[int, Field(description="ソースのチャンネル番号 (1始まり)", ge=1)],
        db: Annotated[float | None, Field(description="目標センド量 (dB)。-100 以下で -inf")] = None,
        delta_db: Annotated[float | None, Field(description="現在値からの相対変化 (dB)")] = None,
        fade_ms: FadeArg = None,
        confirm: ConfirmArg = False,
    ) -> dict[str, Any]:
        """ソースから AUX バスへのセンド量を設定する。上げる場合は自動でゆっくりフェード (ハウリング対策)。"""

        def op() -> dict[str, Any]:
            target = db if db is None or db > -100 else float("-inf")
            return ctl.set_aux_send(bus, ctl.channel(source_type, source), target, delta_db, fade_ms, confirm)

        return await run(op)

    @mcp.tool(annotations=WRITE)
    async def set_aux_send_mute(
        bus: BusArg,
        source_type: SourceTypeArg,
        source: Annotated[int, Field(ge=1)],
        muted: bool,
    ) -> dict[str, Any]:
        """AUX センドをミュート/解除する。解除はゆっくり元のレベルへ戻す。"""
        return await run(lambda: ctl.set_aux_send_mute(bus, ctl.channel(source_type, source), muted))

    @mcp.tool(annotations=WRITE)
    async def set_aux_send_pan(
        bus: BusArg,
        source_type: SourceTypeArg,
        source: Annotated[int, Field(ge=1)],
        pan: Annotated[float, Field(ge=-100, le=100)],
    ) -> dict[str, Any]:
        """ステレオリンクされた AUX へのセンドのパンを設定する (-100〜+100)。"""
        return await run(lambda: ctl.set_aux_send_pan(bus, ctl.channel(source_type, source), pan))

    @mcp.tool(annotations=WRITE)
    async def set_aux_send_tap(
        bus: BusArg,
        source_type: SourceTypeArg,
        source: Annotated[int, Field(ge=1)],
        mode: Annotated[
            Literal["pre", "post", "pre_proc", "post_proc"],
            Field(description="pre/post: フェーダー前/後。pre_proc/post_proc: EQ・ダイナミクス前/後"),
        ],
    ) -> dict[str, Any]:
        """AUX センドの取り出し位置 (PRE/POST, PRE PROC/POST PROC) を設定する。"""
        return await run(lambda: ctl.set_aux_send_tap(bus, ctl.channel(source_type, source), mode))

    # --- EQ -----------------------------------------------------------------

    @mcp.tool(annotations=READ)
    async def get_eq(channel_type: EqTypeArg, channel: Annotated[int, Field(ge=1)]) -> dict[str, Any]:
        """チャンネルの 4 バンドパラメトリック EQ とハイパスフィルターの設定を返す。"""
        return await run(lambda: ctl.get_eq(ctl.channel(channel_type, channel)))

    @mcp.tool(annotations=WRITE)
    async def set_eq_band(
        channel_type: EqTypeArg,
        channel: Annotated[int, Field(ge=1)],
        band: Annotated[int, Field(description="バンド 1〜4 (低→高)", ge=1, le=4)],
        frequency_hz: Annotated[float | None, Field(description="中心周波数 20〜22050 Hz")] = None,
        q: Annotated[float | None, Field(description="Q 0.05〜15 (大きいほど狭い)")] = None,
        gain_db: Annotated[float | None, Field(description="ゲイン -20〜+20 dB")] = None,
        confirm: ConfirmArg = False,
    ) -> dict[str, Any]:
        """EQ バンドを設定する。ブースト方向のゲイン変更はゆっくり行う。カットは即時。"""

        def op() -> dict[str, Any]:
            ch = ctl.channel(channel_type, channel)
            return ctl.set_eq_band(ch, band, frequency_hz, q, gain_db, confirm)

        return await run(op)

    @mcp.tool(annotations=WRITE)
    async def set_eq_bypass(
        channel_type: EqTypeArg, channel: Annotated[int, Field(ge=1)], bypass: bool
    ) -> dict[str, Any]:
        """EQ のバイパス (true で EQ オフ)。"""
        return await run(lambda: ctl.set_eq_bypass(ctl.channel(channel_type, channel), bypass))

    @mcp.tool(annotations=WRITE)
    async def set_hpf(
        channel_type: EqTypeArg,
        channel: Annotated[int, Field(ge=1)],
        frequency_hz: Annotated[
            float | None, Field(description="カットオフ 20〜1000 Hz。null でハイパスをオフ")
        ] = None,
    ) -> dict[str, Any]:
        """ハイパスフィルターを設定する (低域カット)。"""
        return await run(lambda: ctl.set_hpf(ctl.channel(channel_type, channel), frequency_hz))

    # --- meters -------------------------------------------------------------

    @mcp.tool(annotations=READ)
    async def get_meters() -> dict[str, Any]:
        """全チャンネルとマスターの現在のメーター値 (dB, -80=無音) を返す。
        入力に信号が来ているか、クリップしていないか、コンプがどれだけ効いているかの確認に使う。
        input/line/player: pre_db (ゲイン後), post_db (EQ/ダイナミクス後), post_fader_db (出力),
        comp_reduction_db (コンプのゲインリダクション)。"""
        return await run(ctl.get_meters)

    @mcp.tool(annotations=READ)
    async def get_spectrum() -> dict[str, Any]:
        """RTA (スペクトラムアナライザ) の最新値を 31 バンドにまとめて返す。対象はミキサー側で
        RTA に選択されているチャンネル (未選択ならマスター)。"""
        return await run(ctl.get_spectrum)

    # --- graphic EQ -----------------------------------------------------------

    @mcp.tool(annotations=READ)
    async def get_graphic_eq(target: GeqTargetArg, aux: GeqAuxArg = None) -> dict[str, Any]:
        """AUX (モニター返し) またはマスターの 31 バンドグラフィック EQ (±15 dB) を返す。"""
        return await run(lambda: ctl.get_graphic_eq(_geq_target(target, aux)))

    @mcp.tool(annotations=WRITE)
    async def set_graphic_eq_band(
        target: GeqTargetArg,
        gain_db: Annotated[float, Field(description="ゲイン -15〜+15 dB", ge=-15, le=15)],
        aux: GeqAuxArg = None,
        band: Annotated[int | None, Field(description="バンド番号 1 (20Hz) 〜 31 (20kHz)", ge=1, le=31)] = None,
        frequency_hz: Annotated[float | None, Field(description="周波数 (最も近いバンドを選ぶ)")] = None,
        side: Annotated[
            Literal["both", "left", "right"], Field(description="マスターで L/R がリンクされていない時の対象")
        ] = "both",
        confirm: ConfirmArg = False,
    ) -> dict[str, Any]:
        """グラフィック EQ の 1 バンドを設定する。ハウリングするモニターの周波数を
        カットする時などに使う。カットは即時、ブーストはゆっくり行う。"""

        def op() -> dict[str, Any]:
            return ctl.set_graphic_eq_band(_geq_target(target, aux), gain_db, band, frequency_hz, side, confirm)

        return await run(op)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
    async def reset_graphic_eq(
        target: GeqTargetArg,
        aux: GeqAuxArg = None,
        side: Literal["both", "left", "right"] = "both",
        confirm: Annotated[
            bool, Field(description="調整済みの EQ (会場の音響補正など) を消すため必須。ユーザーが望んだ時だけ true")
        ] = False,
    ) -> dict[str, Any]:
        """グラフィック EQ をフラットに戻す (confirm=true が必要)。ブーストしていたバンドは即時に戻し、
        カットしていたバンドはゆっくり戻す。"""
        return await run(lambda: ctl.reset_graphic_eq(_geq_target(target, aux), side, confirm))

    @mcp.tool(annotations=WRITE)
    async def set_graphic_eq_bypass(target: GeqTargetArg, bypass: bool, aux: GeqAuxArg = None) -> dict[str, Any]:
        """グラフィック EQ のバイパス (true でオフ)。"""
        return await run(lambda: ctl.set_graphic_eq_bypass(_geq_target(target, aux), bypass))

    def _geq_target(target: str, aux: int | None):  # noqa: ANN202
        return ctl.channel("master") if target == "master" else ctl.channel("aux", aux)

    # --- dynamics -----------------------------------------------------------

    @mcp.tool(annotations=READ)
    async def get_dynamics(channel_type: DynTypeArg, channel: Annotated[int, Field(ge=1)]) -> dict[str, Any]:
        """コンプレッサーとゲートの設定 (ミキサー内部の 0〜1 正規化値) を返す。"""
        return await run(lambda: ctl.get_dynamics(ctl.channel(channel_type, channel)))

    @mcp.tool(annotations=WRITE)
    async def set_compressor(
        channel_type: DynTypeArg,
        channel: Annotated[int, Field(ge=1)],
        threshold: Norm = None,
        ratio: Norm = None,
        attack: Norm = None,
        release: Norm = None,
        gain: Norm = None,
        outgain: Norm = None,
        softknee: Annotated[bool | None, Field(description="ソフトニー")] = None,
    ) -> dict[str, Any]:
        """コンプレッサーを設定する。値はミキサー内部の正規化値 (0〜1)。get_dynamics で現在値を
        確認し、少しずつ変えること。gain/outgain を上げる変更はゆっくり行う。"""
        params: dict[str, float] = {
            k: v
            for k, v in {
                "threshold": threshold,
                "ratio": ratio,
                "attack": attack,
                "release": release,
                "gain": gain,
                "outgain": outgain,
            }.items()
            if v is not None
        }
        if softknee is not None:
            params["softknee"] = 1.0 if softknee else 0.0
        return await run(lambda: ctl.set_compressor(ctl.channel(channel_type, channel), params))

    @mcp.tool(annotations=WRITE)
    async def set_gate(
        channel_type: DynTypeArg,
        channel: Annotated[int, Field(ge=1)],
        thresh: Norm = None,
        depth: Norm = None,
        attack: Norm = None,
        hold: Norm = None,
        release: Norm = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        """ノイズゲートを設定する。値はミキサー内部の正規化値 (0〜1)。"""
        values = {"thresh": thresh, "depth": depth, "attack": attack, "hold": hold, "release": release}
        params = {k: v for k, v in values.items() if v is not None}
        return await run(lambda: ctl.set_gate(ctl.channel(channel_type, channel), params, enabled))

    @mcp.tool(annotations=WRITE)
    async def set_dynamics_bypass(
        channel_type: DynTypeArg,
        channel: Annotated[int, Field(ge=1)],
        section: Literal["compressor", "gate"],
        bypass: bool,
    ) -> dict[str, Any]:
        """コンプレッサーまたはゲートをバイパス (true でオフ) する。"""
        return await run(lambda: ctl.set_dynamics_bypass(ctl.channel(channel_type, channel), section, bypass))

    mcp.mixer = mixer  # type: ignore[attr-defined]
    mcp.controller = ctl  # type: ignore[attr-defined]
    return mcp
