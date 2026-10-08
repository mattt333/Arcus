# Market maker Arcus

Un seul fichier `main.py`, Python 3.11+, deux dépendances. Testnet par défaut.

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:ARCUS_ADDRESS = "0x..."
$env:ARCUS_SIGNING_KEY = "..."
$env:ARCUS_ACCOUNT_INDEX = "0"
.venv\Scripts\python.exe main.py
```

Créer et autoriser la clé sur la page **API Keys** d'Arcus. `ARCUS_SIGNING_KEY`
est l'**API Signing Key** privée Ed25519, pas la clé du wallet Ethereum ni la clé
publique API. Alimenter le sous-compte et le dédier au bot : aucun autre ordre
sur les paires configurées. Ne pas enregistrer la clé dans le code.

Pour le mainnet, définir `ARCUS_BASE_URL=https://api.arcus.xyz` et utiliser les
identifiants de cet environnement. Le lancement envoie réellement des ordres.

Paramètres en haut de `main.py` :

- `PAIRS` : 10 $ de **notionnel** par ordre, position nette limitée à ±40 $ par
  paire. À ×20, cela représente environ 0,50 $ de marge par ordre, hors frais
  et règles de marge. Ajouter une entrée pour chaque nouvelle paire.
- Le levier maximum est calculé depuis `initialMarginFraction` : ×20 pour NVDA.
  Le bot attend la confirmation du changement avant de placer ses ordres.
- `BOOK_LEVELS=5`, `MIN_SIDE_SHARE=0.20` : liquidité mesurée en notionnel sur les
  cinq meilleurs niveaux, en retirant les propres ordres actifs du bot.
  Un côté sous 20 % de la liquidité totale est désactivé : bids faibles →
  annulation des achats ; asks faibles → annulation des ventes.

Un ordre BUY au meilleur bid et un SELL au meilleur ask, en **ALO/post-only**.
Un changement de meilleur prix, un manque de liquidité ou une taille qui
dépasserait la limite déclenche une annulation. Le remplacement attend le
statut terminal, et les nouveaux placements attendent que la position reflète
les fills reçus. Les tailles sont arrondies vers le bas au pas du marché ;
aucun ordre sous les minimums Arcus n'est envoyé. Un remplissage partiel ne
déclenche pas de complément automatique.

La limite porte sur la position nette existante et le remplissage possible de
l'ordre dans chaque sens. Le cours peut faire dépasser 40 $ après un fill :
le bot bloque les nouveaux ordres qui augmenteraient l'exposition, sans fermer
la position de force. Les positions restent ouvertes quand le bot s'arrête.

Le carnet est reçu par snapshots WebSocket (~200 ms). Si aucun snapshot frais
n'arrive pendant 2 secondes, ou en cas d'erreur, le bot s'arrête et tente
d'annuler ses ordres. `Ctrl+C` déclenche aussi cette annulation. Un switch
serveur par paire, renouvelé toutes les 10 secondes avec une échéance de
30 secondes, annule les ordres du marché si le processus ou la connexion
disparaît. Il concerne tous les ordres de cette paire sur le sous-compte.
Pas de reconnexion automatique : corriger l'erreur puis relancer.

Vérifications locales, sans envoi d'ordres :

```powershell
.venv\Scripts\python.exe -m unittest -v
```

Documentation : https://docs.arcus.xyz/guides/websocket-trading
