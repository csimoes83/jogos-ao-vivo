# jogos-ao-vivo

Liga o «ao vivo» e atualiza os resultados dos jogos da Liga Placard, Liga Feminina Placard e Liga Prime Futsal
no futsalportugal.com (24-26/10/2026), a partir de um GitHub Action. Não depende de nenhum computador ligado.

- `live.py` — o programa (só biblioteca standard do Python). `python3 live.py --selftest` corre os testes.
- `matches.json` — os jogos, com a hora de Lisboa, o id no Flashscore e (Espanha) a página da RFEF.
- `.github/workflows/live.yml` — corre de 10 em 10 minutos; também se pode lançar à mão
  (Actions → «jogos ao vivo» → Run workflow): `probe` só lê, `dry` pede dry-run ao site, `run` aplica.

Fontes: Flashscore (Portugal e Espanha) e RFEF (confirmação em Espanha). A FPF bloqueia os servidores do GitHub.
Segredo necessário: `NFP_MCP_TOKEN` (token do site com permissões de leitura e escrita MCP, sem admin).

## Champions de futsal (eliminatórias): operador nativo da UEFA

Os jogos de `matches.json` com o campo `"uefa": <id do jogo na API da UEFA>` são seguidos **só pela API pública da UEFA**
(`uefa/uefa_op.py`), nunca pelo Flashscore. O que faz (de 20 em 20 segundos, sem o Mac ligado):

- ao pontapé de saída liga o «ao vivo» e põe 0-0; mantém o marcador; no fim grava o resultado, desliga o «ao vivo» e recalcula a classificação (competição 2853);
- `part=2` e faltas a zero no início da 2.ª parte; faltas de equipa da parte (0 a 5) enquanto decorre;
- golos e cartões por id UEFA (`external_ids`, source `uefa`), só de jogadores já ligados; nunca cria pessoas, nunca apaga;
- textos nas incidências, em 5 línguas, `extra="auto"`: início, golos (sempre com texto, marcador corrente e assistência), defesas a remates,
  penáltis falhados, 5.ª falta, intervalo, início da 2.ª parte e fim. Cartões nunca em texto. Nunca repete uma linha que já exista
  (confere `match_id + event_id + minuto + equipa` e o tipo de texto antes de escrever).

**Não faz** (fica no Mac, com `import_sheets`, que precisa de admin): fichas completas (convocados, cinco inicial, treinadores, árbitros),
pessoas novas, auto-golos como evento do jogador (o texto e o marcador saem), cartões a treinadores ou sem jogador na UEFA,
prolongamento e penáltis (avisa e não adivinha), pausas técnicas e bolas nos postes (a UEFA não os publica).

### Interruptor (evita escritas em duplicado com o Mac)

O operador só atua num jogo depois do instante `uefa_from_utc` desse jogo; sem ele vale `uefa_from_utc_default` (topo de `matches.json`).
Tudo o que é `2099-01-01T00:00:00Z` está **desligado**. Para ligar, troca-se a data por uma real, em UTC:

- um jogo: `"uefa_from_utc": "2026-11-24T14:00:00Z"` na entrada desse jogo (ex.: 66611);
- todos os que não têm campo próprio (a 2.ª mão, 66617-66624): `"uefa_from_utc_default": "2026-12-03T00:00:00Z"` na linha do topo.

`--ignore-window` nunca abre o interruptor em `run`. Em `probe` e `dry` o jogo é lido na mesma, para ver o que o operador faria.

### Ficheiros

- `uefa/uefa_op.py` — o operador (e os seus testes, chamados por `live.py --selftest`; fixtures reais em `uefa/testdata/`).
- `uefa/mapa.json` — jogo do site → [id UEFA, equipa da casa, equipa de fora, competição] (1.ª mão 66609-66616, 2.ª mão 66617-66624) e equipas.
- `uefa/uefa_ficha.py` — ficha (golos/cartões) pela UEFA, usada pelo caminho antigo da fase de grupos.
- `descobrir.py` — não liga ao Flashscore jogos da competição 2853.
