"""
main.py — Entry point do sistema ImobOne-v2 (container agents)

Responsabilidades:
  1. Carrega variáveis de ambiente do .env
  2. Conecta ao Redis (StateBoard)
  3. Aguarda em modo de serviço — processa onboardings da fila quando chegam
     (em produção: consome de uma fila Redis ou aguarda chamada via API)

Para setup de um cliente específico, use setup_pipeline.py diretamente:
    python setup_pipeline.py --client-id <id>

Este main.py é o entrypoint do container em modo de serviço contínuo.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

# Carrega .env se existir (local dev; no VPS as vars vêm do env_file do compose)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=False)
except ImportError:
    pass  # python-dotenv opcional em produção

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")


async def _check_redis() -> bool:
    """Verifica conectividade com o Redis na inicialização."""
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    try:
        from state.board import StateBoard
        board = StateBoard(redis_url=redis_url)
        await board.connect()
        ok = await board.ping()
        await board.close()
        return ok
    except Exception as exc:
        logger.error("Redis inacessível em '%s': %s", redis_url, exc)
        return False


async def main() -> None:
    logger.info("ImobOne-v2 — iniciando sistema de agentes")
    logger.info("Redis URL: %s", os.getenv("REDIS_URL", "redis://localhost:6379"))

    # Verifica Redis
    if not await _check_redis():
        logger.critical("Redis não acessível. Encerrando.")
        sys.exit(1)
    logger.info("Redis: OK")

    # Sinaliza que está pronto (lido pelo healthcheck)
    logger.info("Sistema pronto. Aguardando onboardings...")

    # Loop de serviço — mantém o container vivo e responsivo a SIGTERM
    stop = asyncio.Event()

    def _handle_signal(sig, _frame):
        logger.info("Sinal %s recebido — encerrando.", signal.Signals(sig).name)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _handle_signal)

    # Em produção: aqui entraria o consumer de uma fila Redis (BRPOP / pub/sub)
    # que despacha setup_pipeline.run_pipeline() para cada onboarding recebido.
    # Por ora, o container fica em standby e o pipeline é acionado via CLI:
    #   docker exec imovel-ai_agents.1.<id> python setup_pipeline.py --client-id <id>
    while not stop.is_set():
        await asyncio.sleep(5)

    logger.info("Sistema encerrado.")


if __name__ == "__main__":
    asyncio.run(main())
