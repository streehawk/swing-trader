"""commands/conviction.py — /conviction handler (add, list, close, transition, etc).

Uses `import bot_commands as _bc` for lazy access to shared `_reply`.
"""
from __future__ import annotations

import bot_commands as _bc


def handle_conviction(parts: list[str]) -> None:
    sub = parts[1].lower() if len(parts) > 1 else "help"

    try:
        from core import conviction_radar as cr
    except Exception as e:
        _bc._reply(f"❌ Módulo conviction não disponível: `{e}`")
        return

    if sub == "help" or sub in ("?", "-h", "--help"):
        _bc._reply(
            "🧠 *Convicção — radar paralelo*\n"
            "_Trackeia posições com o motor do DipRadar mas sem contaminar o ML._\n\n"
            "*Comandos:*\n"
            "  `/conviction add TICKER PREÇO SHARES TIPO [PICO]`\n"
            "    Tipos: `SWING`, `DIP`, `MOMENTUM`, `CORE`. CORE → never_sell automático.\n"
            "    Auto-faz `/buy` se o ticker não está na carteira.\n"
            "  `/conviction plan TICKER ENTRY STOP TARGET [DIAS]`\n"
            "    Anexa plano swing (marca a posição como SWING). Saídas mecânicas:\n"
            "    stop → breakeven a +1R → trail 1.5×ATR a +1.5R, parcial 50% no alvo,\n"
            "    time stop aos DIAS (default 15 sessões).\n"
            "  `/conviction list` — todas as activas + P&L (🔒 = never_sell)\n"
            "  `/conviction TICKER` — deep-dive condensado (sinais hold/close)\n"
            "  `/conviction close TICKER PREÇO [motivo]` — fechar + gravar outcome\n"
            "  `/conviction delete TICKER` — remover sem outcome (corrigir enganos)\n"
            "  `/conviction transition TICKER TIPO` — mudar tipo (DIP→MOMENTUM etc.)\n"
            "  `/conviction lock TICKER` — never_sell=True (triggers de fase só alertam)\n"
            "  `/conviction unlock TICKER` — never_sell=False (stop ATR pode fechar)\n"
            "  `/conviction sync` — varrer carteira e criar convicções em falta\n"
            "  `/conviction outcomes` — histórico de saídas\n\n"
            "_Auto-sync diário às 06:00 Lisboa. ETFs/PPR/foreign-listed saltados._"
        )
        return

    if sub == "add":
        if len(parts) < 6:
            _bc._reply("⚠️ Uso: `/conviction add TICKER PREÇO SHARES TIPO [PICO]`\n"
                       "_Tipos: DIP, MOMENTUM, CORE_")
            return
        ticker = parts[2].upper().strip()
        try:
            price  = float(parts[3])
            shares = float(parts[4])
        except ValueError:
            _bc._reply("⚠️ Preço e shares têm de ser números.")
            return
        ptype = parts[5].upper().strip()
        if ptype not in cr.VALID_TYPES:
            _bc._reply(f"⚠️ Tipo inválido `{ptype}`. Usa: {', '.join(cr.VALID_TYPES)}")
            return
        trailing_high = None
        if len(parts) >= 7:
            try:
                trailing_high = float(parts[6])
            except ValueError:
                _bc._reply("⚠️ PICO tem de ser um número.")
                return
        try:
            cr.add_conviction(ticker, price, shares, ptype, trailing_high=trailing_high)
        except ValueError as e:
            _bc._reply(f"⚠️ {e}")
            return
        except Exception as e:
            _bc._reply(f"❌ Erro ao adicionar: `{e}`")
            return

        buy_tail = ""
        try:
            from portfolio.portfolio import get_position, buy as portfolio_buy
            if get_position(ticker) is None:
                _br = portfolio_buy(ticker, price, shares)
                buy_tail = (
                    f"\n\n💼 _Também adicionado à carteira: "
                    f"{shares:g}x @ ${price:.2f} (liquidez: €{_br.get('liquidity', 0):.2f})_"
                )
            else:
                buy_tail = (
                    f"\n\n💼 _{ticker} já na carteira — não duplicado. "
                    f"Se queres fazer DCA usa `/buy {ticker} PREÇO SHARES`._"
                )
        except Exception as e:
            buy_tail = f"\n\n⚠️ _Auto-/buy falhou: {e}. Faz `/buy {ticker} {price} {shares}` manual se quiseres._"

        try:
            dive = cr.render_deep_dive(ticker)
            _bc._reply(f"✅ *Convicção aberta — {ticker}*{buy_tail}\n\n{dive}")
        except Exception as e:
            _bc._reply(
                f"✅ *Convicção aberta — {ticker}* ({ptype}){buy_tail}\n"
                f"_(deep-dive falhou: {e}. Tenta `/conviction {ticker}`.)_"
            )
        return

    if sub == "list":
        _bc._reply(cr.render_list())
        return

    if sub == "outcomes":
        _bc._reply(cr.render_outcomes())
        return

    if sub == "delete":
        if len(parts) < 3:
            _bc._reply("⚠️ Uso: `/conviction delete TICKER`\n"
                       "_Remove sem gravar outcome. Para fechar uma trade real usa `close`._")
            return
        ticker = parts[2].upper().strip()
        try:
            removed = cr.delete_conviction(ticker)
        except Exception as e:
            _bc._reply(f"❌ Erro: `{e}`")
            return
        if not removed:
            _bc._reply(f"⚠️ Convicção *{ticker}* não encontrada.")
            return
        _bc._reply(
            f"🗑️ *{ticker} removida* (sem outcome gravado).\n"
            f"_Podes voltar a `/conviction add {ticker} …` com os valores correctos._"
        )
        return

    if sub in ("lock", "unlock"):
        if len(parts) < 3:
            _bc._reply(f"⚠️ Uso: `/conviction {sub} TICKER`")
            return
        ticker = parts[2].upper().strip()
        new_val = (sub == "lock")
        try:
            updated = cr.set_never_sell(ticker, new_val)
        except Exception as e:
            _bc._reply(f"❌ Erro: `{e}`")
            return
        if not updated:
            _bc._reply(f"⚠️ Convicção *{ticker}* não encontrada.")
            return
        if new_val:
            _bc._reply(
                f"🔒 *{ticker} bloqueada — never_sell = TRUE*\n"
                f"_Triggers de fase (trailing stop, momentum cooling) viram alertas 🟡 — "
                f"não recomendam fechar. Triggers estruturais (deterioração de tese, 8-K "
                f"adverso, queda de qualidade) **continuam** a recomendar fechar._"
            )
        else:
            _bc._reply(
                f"🔓 *{ticker} desbloqueada — never_sell = FALSE*\n"
                f"_Triggers de fase podem recomendar fechar. Stop ATR activo._"
            )
        return

    if sub == "sync":
        try:
            result = cr.auto_sync_from_portfolio(send_telegram=None)
        except Exception as e:
            _bc._reply(f"❌ Erro no auto-sync: `{e}`")
            return

        port_count = result.get("portfolio_count", 0)
        if port_count == 0:
            _bc._reply(
                "🔄 *Auto-sync — carteira vazia*\n\n"
                "_O `portfolio.get_positions()` devolveu 0 entradas — não há nada para sincronizar._\n\n"
                "*Como popular a carteira:*\n"
                "  1️⃣ `/conviction add TICKER PREÇO SHARES TIPO` — auto-faz `/buy` também\n"
                "  2️⃣ `/buy TICKER PREÇO SHARES` — auto-cria convicção também\n"
                "  3️⃣ Definir env vars `HOLDING_X=TICKER:SHARES:PREÇO` no Railway e reiniciar\n\n"
                "_Vê o que tens com `/portfolio`._"
            )
            return

        lines = [
            f"🔄 *Auto-sync resultado* (carteira: {port_count} posições)",
            f"  ✅ Adicionadas: {result['added']}",
            f"  ⏭️ Saltadas: {result['skipped']} (ETFs/foreign/erro)",
            f"  ♻️ Já trackeadas: {result['already_tracked']}",
            "",
        ]
        for item in result.get("items", []):
            t, p, r = item
            if r == "added":
                lines.append(f"  ✅ {t} → {p}")
            elif r == "já em convicção":
                lines.append(f"  ♻️ {t} _(já em convicção)_")
            else:
                lines.append(f"  ⏭️ {t} _({r})_")
        _bc._reply("\n".join(lines))
        return

    if sub == "close":
        if len(parts) < 4:
            _bc._reply("⚠️ Uso: `/conviction close TICKER PREÇO [motivo]`")
            return
        ticker = parts[2].upper().strip()
        try:
            price = float(parts[3])
        except ValueError:
            _bc._reply("⚠️ Preço tem de ser um número.")
            return
        reason = " ".join(parts[4:]).strip() if len(parts) >= 5 else "manual"
        try:
            outcome = cr.close_conviction(ticker, price, reason=reason)
        except Exception as e:
            _bc._reply(f"❌ Erro ao fechar: `{e}`")
            return
        if not outcome:
            _bc._reply(f"⚠️ Convicção *{ticker}* não encontrada.")
            return
        pnl_emo = "🟢" if outcome["pnl_pct"] >= 0 else "🔴"
        _bc._reply(
            f"{pnl_emo} *Convicção fechada — {ticker}*\n\n"
            f"  💰 {outcome['shares']:g}x @ ${outcome['exit_price']:.2f}\n"
            f"  📊 P&L: *${outcome['pnl_abs']:+.2f}* ({outcome['pnl_pct']:+.1f}%) em {outcome['days_held']}d\n"
            f"  🏷️ Motivo: _{outcome['reason']}_\n\n"
            f"_Gravado em `conviction_outcomes.json` (não entra em ML)._"
        )
        return

    if sub == "plan":
        if len(parts) < 5:
            _bc._reply(
                "⚠️ Uso: `/conviction plan TICKER ENTRY STOP TARGET [DIAS]`\n"
                "_Ex: `/conviction plan AAPL 231.40 224.10 246.20`_\n"
                "_DIAS = time stop em sessões (default 15)._"
            )
            return
        ticker = parts[2].upper().strip()
        try:
            entry  = float(parts[3])
            stop   = float(parts[4])
            target = float(parts[5]) if len(parts) > 5 else 0.0
        except ValueError:
            _bc._reply("⚠️ ENTRY, STOP e TARGET têm de ser números.")
            return
        if len(parts) < 6:
            _bc._reply("⚠️ Falta o TARGET. Uso: `/conviction plan TICKER ENTRY STOP TARGET [DIAS]`")
            return
        try:
            days = int(parts[6]) if len(parts) > 6 else 15
        except ValueError:
            _bc._reply("⚠️ DIAS tem de ser um inteiro.")
            return
        try:
            pos = cr.set_swing_plan(ticker, entry, stop, target, time_stop_days=days)
        except Exception as e:
            _bc._reply(f"❌ Erro: `{e}`")
            return
        if not pos:
            _bc._reply(f"⚠️ Convicção *{ticker}* não encontrada. Cria com `/conviction add` primeiro.")
            return
        _r = entry - stop
        _rr = (target - entry) / _r if _r > 0 else 0.0
        _bc._reply(
            f"⚡ *{ticker}* → SWING\n"
            f"`entry  {entry:>9.2f}`\n"
            f"`stop   {stop:>9.2f}  ({(stop/entry-1)*100:+.1f}% = 1R = ${_r:.2f})`\n"
            f"`target {target:>9.2f}  (R:R {_rr:.1f})`\n"
            f"`time   {days} sessões`\n\n"
            f"_Breakeven a +1R · trail 1.5×ATR a +1.5R · parcial 50% no alvo._"
        )
        return

    if sub == "transition":
        if len(parts) < 4:
            _bc._reply("⚠️ Uso: `/conviction transition TICKER TIPO`")
            return
        ticker = parts[2].upper().strip()
        new_type = parts[3].upper().strip()
        if new_type not in cr.VALID_TYPES:
            _bc._reply(f"⚠️ Tipo inválido `{new_type}`. Usa: {', '.join(cr.VALID_TYPES)}")
            return
        try:
            pos = cr.transition_type(ticker, new_type)
        except Exception as e:
            _bc._reply(f"❌ Erro: `{e}`")
            return
        if not pos:
            _bc._reply(f"⚠️ Convicção *{ticker}* não encontrada.")
            return
        if new_type == "MOMENTUM":
            _bc._reply(f"🔄 *{ticker}* → {new_type}\n_Trailing high ancorado a ${pos.trailing_high:.2f}._")
        else:
            _bc._reply(f"🔄 *{ticker}* → {new_type}")
        return

    # Default: treat first arg as TICKER → deep-dive
    ticker = parts[1].upper().strip()
    try:
        _bc._reply(cr.render_deep_dive(ticker))
    except Exception as e:
        _bc._reply(f"❌ Erro no deep-dive de {ticker}: `{e}`")
