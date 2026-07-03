# run_sam.py
"""
Script de carga para medição de emissões via CodeCarbon.
Executa a suíte de testes real do projeto (sem cobertura/relatórios,
para não distorcer a medição de consumo com overhead de instrumentação).
"""

import os
import subprocess
import sys
from pathlib import Path

# Configuração, ajuste apenas se necessário.

# Diretório raiz do projeto, os testes vão começar a execução a partir dele.
PROJETO = "."

# Diretório dos testes detectado automaticamente, mas pode forçar manualmente
# Exemplos: TESTES = "./tests"  ou  TESTES = "./test"
TESTES = None

# Detecção automática do diretório de testes
CANDIDATOS = ["tests/translator", "tests", "test", "src/tests", "src/test"]

# Necessário: alguns testes do samtranslator checam região AWS configurada
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")


def main():
    global TESTES

    if TESTES is None:
        for candidato in CANDIDATOS:
            if Path(candidato).exists():
                TESTES = candidato
                break

    if TESTES is None:
        print("Erro: diretório de testes não encontrado.")
        print(f"Procurado em: {CANDIDATOS}")
        print("Defina manualmente a variável TESTES no script.")
        sys.exit(1)

    print(f"Projeto : {os.path.abspath(PROJETO)}")
    print(f"Testes  : {TESTES}")
    print()

    resultado = subprocess.run(
        [
            sys.executable, "-m", "pytest", TESTES,
            "-q",
        ],
        cwd=PROJETO,
        text=True,
        encoding="utf-8",
    )

    print(f"\nExit code: {resultado.returncode}")
    sys.exit(resultado.returncode)


if __name__ == "__main__":
    main()