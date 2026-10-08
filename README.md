## smooth-operator

Extrai campos parametrizáveis de PDFs, planilhas e textos usando llama.cpp.

Tudo via linha de comando: o script sobe um llama-server headless (sem UI),
processa os arquivos, grava o resultado e desliga o servidor.

Uso:
```bash
python extrair.py input/ --modelo [modelo]--campos _campos.json --saida output/[output].csv
```
Dependências (opcionais conforme os formatos de input/output usados):
```bash
pip install pymupdf4llm pandas openpyxl
```
Uso como biblioteca:

```py
from extrair import extrair_campos
extrair_campos(
    texto, {"titulo": "string", "valor_total": "number"}, 
    url="http://127.0.0.1:8080"
)
```
