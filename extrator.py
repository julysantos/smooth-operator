import argparse
import atexit
import csv
import json
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TIPOS = {"string", "number", "integer", "boolean"}
SUFIXOS = {".pdf", ".xlsx", ".xlsm", ".xls", ".csv", ".txt", ".md"}

def normalizar_campos(campos):
    """Aceita {"nome": "string"} ou {"nome": {"tipo": "...", "descricao": "..."}}."""
    saida = {}
    for nome, definicao in campos.items():
        if isinstance(definicao, str):
            definicao = {"tipo": definicao}
        tipo = definicao.get("tipo", "string")
        if tipo not in TIPOS:
            raise ValueError(f"Campo '{nome}': tipo '{tipo}' inválido. Use {sorted(TIPOS)}.")
        saida[nome] = {"tipo": tipo, "descricao": definicao.get("descricao", "")}
    return saida


def montar_schema(campos):
    """Todos os campos aceitam null, para o modelo não alucinar ou inventar valores."""
    props = {}
    for nome, c in campos.items():
        prop = {"anyOf": [{"type": c["tipo"]}, {"type": "null"}]}
        if c["descricao"]:
            prop["description"] = c["descricao"]
        props[nome] = prop
    return {
        "type": "object",
        "properties": props,
        "required": list(campos),
        "additionalProperties": False,
    }

def ler_documento(caminho: Path) -> str:
    sufixo = caminho.suffix.lower()
    if sufixo == ".pdf":
        try:
            import pymupdf4llm

            return pymupdf4llm.to_markdown(str(caminho))
        except ImportError:
            try:
                r = subprocess.run(
                    ["pdftotext", "-layout", str(caminho), "-"],
                    capture_output=True, text=True, encoding="utf-8", check=True,
                )
            except FileNotFoundError:
                raise RuntimeError(
                    "pymupdf4llm não instalado neste Python e `pdftotext` não está no PATH "
                    f"(Python em uso: {sys.executable})"
                ) from None
            return r.stdout
    if sufixo in {".xlsx", ".xlsm", ".xls"}:
        import pandas as pd

        abas = pd.read_excel(caminho, sheet_name=None)
        return "\n\n".join(
            f"## Aba: {nome}\n{aba.to_csv(index=False)}" for nome, aba in abas.items()
        )
    return caminho.read_text(encoding="utf-8", errors="replace")


def dividir(texto, tam, sobreposicao=400):
    """Divide em blocos de ~tam caracteres, preferindo cortar em quebras de linha."""
    if len(texto) <= tam:
        return [texto]
    partes, i = [], 0
    while i < len(texto):
        fim = min(i + tam, len(texto))
        if fim < len(texto):
            corte = texto.rfind("\n", i + tam // 2, fim)
            if corte != -1:
                fim = corte
        partes.append(texto[i:fim])
        if fim >= len(texto):
            break
        i = max(fim - sobreposicao, i + 1)
    return partes

class Servidor:
    def __init__(self, binario, modelo, ctx, ngl, porta, extra, log="llama-server.log"):
        self.url = f"http://127.0.0.1:{porta}"
        self.log_path = log
        cmd = [
            binario, "-m", modelo, "-c", str(ctx), "-ngl", str(ngl),
            "-np", "1", "--jinja", "--no-webui",
            "--host", "127.0.0.1", "--port", str(porta),
        ] + shlex.split(extra)
        self.log = open(log, "w")
        self.proc = subprocess.Popen(cmd, stdout=self.log, stderr=subprocess.STDOUT)
        atexit.register(self.parar)

    def esperar(self, limite=300):
        inicio = time.time()
        while time.time() - inicio < limite:
            if self.proc.poll() is not None:
                self.log.flush()
                cauda = Path(self.log_path).read_text().splitlines()[-15:]
                sys.exit("llama-server encerrou ao iniciar:\n" + "\n".join(cauda))
            try:
                with urllib.request.urlopen(self.url + "/health", timeout=2) as r:
                    if r.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                pass  # 503 enquanto carrega o modelo, ou ainda sem conexão
            time.sleep(1)
        sys.exit("Tempo esgotado esperando o llama-server carregar o modelo.")

    def parar(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

def chamar_llm(url, system, user, schema, max_tokens=1024, timeout=900):
    corpo = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "extracao", "strict": True, "schema": schema},
        },
        # Desliga o raciocínio do Qwen3 (outros modelos ignoram).
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        url + "/v1/chat/completions",
        data=json.dumps(corpo).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.load(r)
    return resp["choices"][0]["message"]["content"]

def extrair_campos(texto, campos, url, chunk_chars=24000, instrucoes=""):
    """Extrai 'campos' de 'texto'. Retorna {campo: valor ou None}.

    Documentos longos são divididos em blocos; vale o primeiro valor não nulo
    encontrado para cada campo, e o processamento para quando todos foram achados.
    """
    campos = normalizar_campos(campos)
    schema = montar_schema(campos)
    descricao = "\n".join(
        f"- {nome} ({c['tipo']})" + (f": {c['descricao']}" if c["descricao"] else "")
        for nome, c in campos.items()
    )
    system = (
        "Você extrai informações de documentos. Responda somente com JSON no esquema "
        "pedido. Use null quando a informação não estiver explícita no trecho; "
        "nunca invente valores. " + instrucoes
    ).strip()

    resultado = {nome: None for nome in campos}
    for parte in dividir(texto, chunk_chars):
        user = f"Campos a extrair:\n{descricao}\n\nDocumento:\n\"\"\"\n{parte}\n\"\"\""
        try:
            dados = json.loads(chamar_llm(url, system, user, schema))
        except (json.JSONDecodeError, KeyError):
            continue
        for nome in campos:
            if resultado[nome] is None and dados.get(nome) is not None:
                resultado[nome] = dados[nome]
        if all(v is not None for v in resultado.values()):
            break
    return resultado


def listar_arquivos(entrada: Path):
    if entrada.is_file():
        return [entrada]
    return sorted(p for p in entrada.rglob("*") if p.suffix.lower() in SUFIXOS)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("entrada", type=Path, help="arquivo ou pasta de documentos")
    ap.add_argument("--campos", type=Path, required=True, help="JSON com os campos a extrair")
    ap.add_argument("--saida", type=Path, default=Path("/output/resultado.csv"))
    ap.add_argument("--modelo", help="caminho do .gguf (necessário sem --url)")
    ap.add_argument("--url", help="usar um llama-server já em execução, ex.: http://127.0.0.1:8080")
    ap.add_argument("--llama-server", default="llama-server", help="caminho do binário")
    ap.add_argument("--porta", type=int, default=8080)
    ap.add_argument("--ctx", type=int, default=16384)
    ap.add_argument("--ngl", type=int, default=99, help="camadas na GPU (reduza para modelos 7-8B)")
    ap.add_argument("--extra-args", default="-fa on -ctk q8_0 -ctv q8_0",
                    help="flags extras do llama-server (troque se sua versão usar outra sintaxe)")
    ap.add_argument("--chunk-chars", type=int, default=24000,
                    help="tamanho dos blocos; ~3,5 caracteres por token em português")
    ap.add_argument("--instrucoes", default="", help="instruções extras para o modelo")
    args = ap.parse_args()

    campos = normalizar_campos(json.loads(args.campos.read_text(encoding="utf-8")))
    arquivos = listar_arquivos(args.entrada)
    if not arquivos:
        sys.exit("Nenhum arquivo suportado encontrado.")

    if args.url:
        url = args.url.rstrip("/")
    else:
        if not args.modelo:
            sys.exit("Informe --modelo ou --url.")
        print("Iniciando llama-server...", file=sys.stderr)
        srv = Servidor(args.llama_server, args.modelo, args.ctx, args.ngl, args.porta, args.extra_args)
        srv.esperar()
        url = srv.url

    colunas = ["arquivo", *campos, "erro"]
    with open(args.saida, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=colunas, delimiter=";")
        w.writeheader()
        for i, caminho in enumerate(arquivos, 1):
            print(f"[{i}/{len(arquivos)}] {caminho.name}", file=sys.stderr)
            linha = {"arquivo": str(caminho), "erro": ""}
            try:
                texto = ler_documento(caminho)
                if not texto.strip():
                    raise ValueError("sem texto extraível (PDF escaneado?)")
                linha.update(extrair_campos(texto, campos, url, args.chunk_chars, args.instrucoes))
            except Exception as e:  # um arquivo com erro não derruba o lote
                linha["erro"] = f"{type(e).__name__}: {e}"
            w.writerow(linha)
            f.flush()
    print(f"Pronto: {args.saida}", file=sys.stderr)


if __name__ == "__main__":
    main()