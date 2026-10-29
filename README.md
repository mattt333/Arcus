# Market making XAU sur Lighter

Bot limité au contrat perpétuel **XAU** de **Lighter sur Robinhood Chain**,
mainnet, chain ID **466324**. La stratégie est dans `main.py`, les appels
Lighter dans `exchanges.py`. Les intégrations Arcus et l'arbitrage de funding
ont été retirés.

## Règles

- À plat : un achat au meilleur bid et une vente au meilleur ask, en
  **LIMIT / POST_ONLY**. Un mouvement du best bid/ask provoque une annulation
  confirmée puis un remplacement. Aucun passage taker pour les quotes.
- Taille cible : **10 $ de notionnel**, avec la même quantité XAU des deux
  côtés pour permettre à l'ordre opposé de solder la position.
- Pour une nouvelle entrée, un tick doit contenir au moins
  **100 $ de liquidité des autres comptes**.
  Toutes les quantités au même prix sont additionnées ; les ordres de notre
  compte sont exclus. Le contrôle porte sur le côté de notre ordre d'entrée.
  À exactement 100 $, l'entrée est conservée ; sous 100 $, elle est annulée.
- Dès un fill, même partiel : annuler le reliquat dans le sens de la
  position et bloquer les nouvelles entrées de ce côté. Il n'y a pas de
  cumul d'ordres pour augmenter l'inventaire.
- Après un fill complet, conserver l'ordre opposé à son prix initial tant
  que la position reste ouverte, même si le meilleur prix se déplace ou
  si la liquidité externe disparaît à son tick.
  Après un fill partiel, si cet ordre peut dépasser l'inventaire restant,
  le recréer au même prix en **reduce-only**, pour la quantité restante
  arrondie au minimum accepté par le marché. Un ordre déjà reduce-only
  peut être conservé même si son reliquat dépasse la position.
- Dès le premier fill, figer le prix de l'ordre opposé pour prendre le
  spread. Une sortie expirée, redimensionnée ou annulée après une erreur
  de lecture est recréée **au même prix**, en POST_ONLY / reduce-only.
  La sortie ne suit jamais le meilleur bid/ask courant. Le stop continue
  à être surveillé pendant toute la position.
- Stop à **1 $ de variation du prix XAU**, par rapport au prix moyen
  d'entrée exécuté : long si le meilleur bid est inférieur ou égal à
  entrée − 1 ; short si le meilleur ask est supérieur ou égal à entrée + 1.
  Exemple : achat à 3000,45 → déclenchement à un bid de 2999,45.
- Au stop : annuler les quotes, confirmer leurs exécutions éventuelles,
  relire la position, puis fermer en **MARKET / IOC / reduce-only**.
  La limite d'exécution autorise 2 % de slippage ; un reliquat est retenté
  sans envoyer une nouvelle fermeture tant que la précédente est incertaine.
- Après confirmation d'une position nulle et annulation des reliquats,
  recommencer à coter dans les deux sens.

Le stop est **géré par le bot**, pas enregistré comme ordre conditionnel sur
Lighter. Il permet de conserver l'ordre opposé existant, puis de l'annuler
avant la fermeture pour éviter un retournement. Le bot doit rester actif
et connecté pour déclencher le stop. Le seuil de déclenchement ne garantit
pas le prix d'exécution.

## Taille de 10 $ et minimums du marché

L'API publique XAU a été consultée pendant la refonte : minimum de **10 $**,
quantité minimale **0,0020 XAU**, pas **0,0001 XAU**, tick de prix **0,01 $**.
Les valeurs sont relues depuis l'API au démarrage.

Par défaut, **10 $ est une taille cible** : le bot utilise le premier pas
de quantité qui atteint 10 $ au bid, avec la même quantité à l'ask.
L'arrondi est activé par défaut (`ROUND_ORDER_UP=1`). Par exemple, autour
de 4020 $, 0,0024 XAU valent environ 9,65 $ et 0,0025 XAU environ 10,05 $.
Dans ce mode, le plafond d'inventaire est la quantité de cette unique paire
de quotes arrondie ; sa valeur peut dépasser 10 $ et fluctue avec le prix.
`ROUND_ORDER_UP=0` réactive le plafond strict de 10 $. Ce mode peut bloquer
les entrées : avec le minimum actuel de 10 $ et deux prix différents pour
une même quantité, les deux quotes ne peuvent pas toutes deux être exactement
à 10 $. Si le service définit déjà cette variable à `0`, la passer à `1`.

Les sorties reduce-only respectent également les minimums de quantité et
de notionnel du marché. Après un fill partiel, la quantité envoyée est le
maximum entre la position restante, `min_base_amount` et
`min_quote_amount / prix`, arrondi au pas supérieur. Par exemple, pour
0,0010 XAU à fermer autour de 4020 $, le bot envoie 0,0025 XAU **reduce-only**.
[Lighter limite les exécutions à la position et annule le surplus](https://docs.lighter.xyz/trading/order-types-and-matching) :
cette quantité supérieure ne peut pas retourner la position. Le journal
conserve la quantité effectivement envoyée et seuls les fills confirmés
modifient l'inventaire attendu. Cette règle couvre aussi les MARKET / IOC
du stop, avec un calcul tenant compte du prix limite de slippage.

## Installation et lancement

Python 3.11–3.13, SDK officiel `lighter-sdk==1.1.6`.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

export LIGHTER_ACCOUNT_INDEX='...'
export LIGHTER_API_KEY_INDEX='...'
export LIGHTER_API_PRIVATE_KEY='...'
export STATE_FILE='xau-mm-state.json'

# Valeur par défaut ; remplace un éventuel ancien réglage à 0 :
export ROUND_ORDER_UP='1'

.venv/bin/python main.py
```

L'API est `https://api.rh.lighter.xyz` ; l'interface est
https://robinhoodchain.lighter.xyz/. Les autres endpoints sont refusés.
Les index de clés autorisés sont 4–156 et 158–254.

Utiliser un compte dédié à ce bot. Au démarrage, les ordres externes et
positions non réconciliées avec le journal bloquent les nouvelles entrées.
Le levier reste en marge isolée, avec une marge initiale cible supérieure
de 50 % au minimum du marché, levier entier arrondi vers le bas.
La configuration de marge est confirmée avant la première entrée.

Les anciennes variables `ARCUS_*`, `POSITION_USD`, `MAX_PAIRS`,
`MIN_FUNDING_GAP` et `MAX_SPREAD` ne sont plus utilisées.
Si `STATE_FILE` désigne encore un journal d'arbitrage, choisir le nouveau
journal XAU. Un journal d'une autre stratégie ou d'un autre compte est refusé.

## Exécution, reprise et arrêt

Le bot interroge le carnet, les ordres et la position par REST, avec une
pause de **0,5 seconde entre les cycles**. Les temps réseau et les délais
de confirmation s'ajoutent à cette pause. Les départs des lectures REST sont
espacés d'au moins **0,5 seconde**, tous endpoints confondus dans ce processus.
Chaque rafraîchissement regroupe le suivi des ordres : une lecture des ordres
actifs, puis une pagination commune de l'historique pour les références absentes.
Les confirmations sont également interrogées au plus toutes les 0,5 seconde.
Un cycle avec ordres actifs, position et carnet prend donc au moins environ
1,5 seconde, hors délais réseau et opérations supplémentaires.
Ce n'est pas un flux WebSocket :
les annulations et le stop interviennent au moment de l'observation.

Un HTTP **429** suspend les lectures REST. Le délai respecte `Retry-After`
(secondes ou date HTTP), avec un minimum progressif de **5, 10, 20, 40 puis
60 secondes** si les refus se répètent ; une lecture réussie réinitialise ce
minimum. La boucle attend au moins **60 secondes** entre tentatives de reprise,
et respecte un `Retry-After` plus long. Elle attend ce délai
avant de reprendre, sans multiplier les lectures refusées. L'envoi d'une
annulation reste tenté via le SDK, avec l'index serveur conservé dans le
journal ou l'identifiant client ; elle reste incertaine jusqu'à une lecture
confirmant son état terminal. Aucune nouvelle quote n'est placée tant que
la réconciliation échoue. Pendant la suspension, le stop géré par le bot
ne peut pas suivre le marché. Cette cadence réduit les requêtes ; elle ne
garantit pas l'absence de 429, notamment si d'autres clients partagent le quota.

Les quotes expirent 6 minutes après leur création. Lighter impose un délai
minimal de 5 minutes ; la minute supplémentaire couvre les délais d'envoi.
La date est transmise en millisecondes Unix. Cette expiration reste un recours
en cas de perte de contact ; les annulations normales sont gérées à chaque cycle.
Une quote expirée peut être recréée après réconciliation.
Le carnet contient au maximum les 250 premiers ordres par côté ; une
liquidité absente du snapshot est traitée comme insuffisante pour une entrée.

`xau-mm-state.json` enregistre les identifiants avant chaque envoi, les
fills confirmés, l'inventaire attendu et une fermeture en cours.
Il est remplacé atomiquement. Une réponse perdue ne provoque jamais un
renvoi automatique du même placement. Tant que fills et position ne
concordent pas, les nouvelles cotations restent suspendues.

Les erreurs de lecture (y compris une réponse HTML ou vide), les ordres
incertains et les écarts persistants suspendent les cotations. Une première
tentative d'annulation des quotes est effectuée, puis le bot vérifie le compte
une fois par minute, sans répéter les annulations tant que cette vérification
échoue. `recovery_pending` conserve cette suspension après un redémarrage.
Une tentative peut comporter plusieurs lectures des ordres et de la position ;
les temps réseau et les vérifications s'ajoutent aux 60 secondes d'attente.
Après réconciliation, le bot vérifie d'abord le stop au prix courant : fermeture
MARKET / IOC / reduce-only si le seuil est atteint, sinon conservation ou
recréation de la sortie LIMIT / POST_ONLY au prix opposé d'origine. À plat,
les cotations habituelles reprennent. Le stop reste indisponible pendant la panne.

Au démarrage, le bot réconcilie le journal avec les ordres ouverts,
l'historique et la position **sans envoyer d'annulation**. Les ordres
réellement présents sont conservés, y compris l'ordre opposé à une position.
Le suivi habituel du marché reprend ensuite ; il peut annuler une quote
d'entrée dont le prix ou la liquidité ne respecte plus les règles de la stratégie.

Le prix opposé de la paire est enregistré dans `opposite_price` avant chaque
envoi d'entrée, même si la première quote est exécutée avant la création de
la seconde. Dès le premier fill, `exit_price` et `exit_side` figent la sortie
du cycle ; l'événement `exit_price_locked` et le message `Prix de sortie figé`
en donnent la trace. Cette cible survit aux expirations et aux redémarrages.
Pour un ancien journal, le bot tente de retrouver la quote opposée d'origine
parmi les ordres enregistrés, en privilégiant un prix du côté bénéficiaire
de l'entrée. S'il ne peut retrouver de cible, il suspend les nouvelles limites
de sortie tout en continuant de surveiller le stop ; il ne choisit pas le
meilleur prix courant comme cible de remplacement.

Un 429, une erreur de lecture, un ordre incertain ou un écart de position
pendant l'initialisation suspend le démarrage dans le même processus, au
lieu de provoquer des redémarrages systemd. Les vérifications sont retentées
après **60 secondes**, ou après le délai REST s'il est plus
long. Un arrêt demandé interrompt cette attente. Les autres erreurs de
configuration ou de compte restent bloquantes et provoquent un échec.
En cas d'écart, le message indique la position signée sur Lighter et celle
attendue dans le journal, en XAU. Une nouvelle lecture peut résoudre un
retard de publication de l'API ; un écart persistant avec une position encore
ouverte nécessite de vérifier le compte et l'historique des exécutions.
Pendant cette attente,
aucune nouvelle cotation n'est envoyée et le stop n'est pas surveillé.
Conserver le journal d'état pour permettre cette réconciliation.

En fonctionnement, si les lectures des ordres et de la position divergent,
le bot effectue jusqu'à **deux nouvelles vérifications**, chacune précédée
d'une pause de **0,5 seconde** (auxquelles s'ajoutent les délais REST).
Aucun nouvel ordre n'est envoyé pendant ces vérifications. Si les lectures
se réconcilient, l'ordre opposé est conservé à son prix figé. Si l'écart
persiste ou si une lecture échoue, les cotations sont suspendues et leur
annulation est tentée. Ces situations produisent un avertissement concis ;
les erreurs inattendues conservent leur pile d'appels. La vérification du
stop reprend après réconciliation ; elle est retardée pendant ces lectures.

Après une fermeture manuelle, si Lighter indique une position nulle mais
que le journal attend encore une position, le démarrage vérifie deux fois
que le compte n'a **aucune position ni aucun ordre actif**, avec au moins
**5 secondes** entre les vérifications. Les quotes introuvables doivent
également avoir dépassé leur expiration (ou le délai de reprise des anciens
journaux). Une fermeture MARKET encore incertaine bloque cette récupération.
Le bot remet alors `expected_size` à zéro, archive l'ancien cycle et reprend
les cotations. L'ajustement et les anciens ordres sont conservés dans
`last_flat_recovery` du journal et l'événement `external_flat_reconciled`
de l'audit. Aucun fichier d'état n'est à supprimer. Une erreur de lecture,
une position ou un ordre actif pendant les vérifications bloque l'ajustement ;
un arrêt demandé interrompt l'attente en conservant l'inventaire local.

Un refus explicite du SDK ou de `sendTx` est enregistré comme `REJECTED`,
avec son message et son code : il ne provoque pas de recherche ou d'annulation
d'un ordre inexistant. Une réponse acceptée conserve le hash de transaction ;
une exception de transport reste incertaine et fait l'objet d'une réconciliation.
Le résultat d'envoi est conservé dans le champ `submission` du journal.

Après un refus explicite d'une quote, les nouvelles tentatives sur le même
côté sont espacées de **5, 10, 20, 40 puis 60 secondes**. Ce délai, conservé
dans `quote_retry`, est réinitialisé après acceptation ou fin du cycle.
Le carnet, les fills et la position continuent d'être relus ; le stop reste
surveillé et ses ordres MARKET ne sont pas différés par cette attente.
Les messages de refus indiquent quantité, prix, notionnel, entiers transmis
au SDK, option reduce-only et minimums du marché pour diagnostiquer un
éventuel nouveau refus `21706`.

Un identifiant enregistré avant l'envoi peut rester `PENDING` sans qu'aucun
ordre ait jamais été créé sur Lighter. Si une quote est introuvable dans
les ordres ouverts et l'historique, le bot attend son expiration plus
5 secondes, puis relit le compte. Les nouvelles quotes conservent leur
date d'expiration dans le journal avant envoi ; cette même date est transmise
au SDK. Lors de la première réconciliation d'un ancien journal sans date
d'expiration, le bot attend une fois **65 secondes après son démarrage**.

Au démarrage initial, la référence peut être classée localement `ABSENT` si les
deux lectures confirment qu'il n'y a aucun ordre ouvert ni aucune position,
et si la position correspond à l'inventaire attendu, ou après la vérification
de compte vide décrite pour une fermeture externe. Cette classification
est conservée dans l'état et l'audit ; elle ne prétend pas qu'une annulation
ou un refus a été confirmé. Aucune annulation n'est envoyée pour cette référence.
Le bot peut ensuite reprendre sans renommer ni supprimer le journal.

Si la réconciliation initiale échoue ou après une panne en fonctionnement,
la reprise traite aussi les quotes expirées introuvables avec une position ou
des ordres connus encore actifs. Elle attend l'expiration plus 5 secondes
(**365 secondes après le démarrage** pour une ancienne quote sans date),
puis effectue deux lectures espacées de 5 secondes. La position et la liste des
références manquantes doivent être stables. La position doit rester dans les
bornes des exécutions possibles des quotes introuvables. Les fills connus sont
conservés ; les références expirées deviennent `ABSENT` et l'inventaire attendu
est ajusté à la position observée, sans inventer d'exécution individuelle.
L'ajustement est enregistré dans `last_api_recovery` et l'événement d'audit
`api_orders_reconciled`. Les sorties existantes sont suivies normalement.
Si le fill d'entrée est introuvable, seul un prix opposé d'origine retrouvé
sans ambiguïté permet de recréer la sortie limite ; le stop reste prioritaire.

Une fermeture MARKET incertaine, un ordre externe, une position incompatible
ou une erreur de lecture bloque la reprise. Une erreur API ne vaut jamais
absence d'ordre. Un arrêt demandé pendant l'attente interrompt la reprise
sans supprimer la référence locale.

Le chemin absolu du journal effectivement utilisé est affiché au démarrage
(`Journal XAU utilisé`). Il peut différer de `xau-mm-state.json` si le service
définit `STATE_FILE`.
Un code `200` accompagné de `{"ratelimit": "didn't use volume quota"}`
est une réponse acceptée avec une information de quota, pas un refus pour
limite de requêtes ni une preuve d'annulation exécutée.

`xau-mm-state.orders.jsonl` conserve les demandes d'ordres, fills,
annulations, déclenchements du stop et cycles terminés.
Un verrou sur `xau-mm-state.lock` interdit deux processus utilisant le même
journal. Garder un seul processus et un seul journal pour ce compte.

`Ctrl+C` / SIGTERM annulent les quotes puis tentent de solder l'inventaire.
Si la fermeture n'est pas confirmée, le journal est conservé et un message
URGENT est affiché. Un arrêt brutal peut laisser une position ouverte.

## Vérifications

```bash
python -m unittest -v
```

La suite utilise des plateformes simulées pour les fills complets et
partiels, limites d'inventaire, seuil de liquidité, stops dans les deux sens,
courses entre fill et annulation, réponses perdues et redémarrages.
Une signature POST_ONLY XAU est aussi construite avec le SDK réel,
hors ligne et avec une clé fictive. Aucun ordre réel n'a été envoyé
pendant ces vérifications.
