# Neon MIDI Rings

Simulateur vertical Pygame où **un rebond joue le prochain événement MIDI** et
**un passage d'ouverture joue uniquement le SFX**. Le monde est calculé en
1080×1920 puis affiché à 50 % dans une fenêtre 540×960.

## Arborescence

```text
project/
├── main.py
├── physics.py
├── recording_audio.py
├── requirements.txt
├── README.md
├── tests/
│   └── test_core.py
├── recordings/
├── music/
│   └── music.mid
├── sounds/
│   └── pass.wav
└── assets/
    └── soundfont.sf2
```

Les trois fichiers média sont volontairement interchangeables et ne sont pas
redistribués : copiez votre MIDI, votre effet et un SoundFont légalement obtenu
aux chemins exacts ci-dessus. Au démarrage, le programme liste précisément tout
fichier manquant. `pass.ogg` fonctionne aussi : modifiez simplement `PASS_SOUND`
en haut de `main.py`.

## Installation Windows

Installez Python 3.11 ou 3.12 64 bits, puis dans PowerShell :

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

`pyfluidsynth` est le module Python, mais il lui faut aussi la bibliothèque
native FluidSynth. Ce projet peut la charger automatiquement depuis
`vendor/fluidsynth/<version>/bin`. Une copie Windows x64 officielle est incluse
dans ce dossier de travail. Option d'installation système avec Chocolatey
(PowerShell administrateur) :

```powershell
choco install fluidsynth
```

Alternative : téléchargez une version Windows 64 bits depuis les releases
FluidSynth, décompressez-la, puis ajoutez son dossier `bin` au `PATH`. Celui-ci
doit contenir `libfluidsynth-3.dll` (le numéro majeur peut varier). Fermez et
rouvrez le terminal après modification du `PATH`. Si Python ne trouve toujours
pas la DLL, copiez le chemin du dossier `bin` dans la variable d'environnement
`PATH` de Windows, pas seulement le chemin de l'exécutable.

Lancement :

```powershell
python main.py
```

## Choisir les médias

- `music/music.mid` : tout MIDI standard type 0 ou 1. Les notes démarrant à
  moins de 30 ms sont regroupées en un accord. La chronologie sert à ordonner
  les notes et calculer leurs durées ; elle ne lance jamais la musique seule.
- `assets/soundfont.sf2` : utilisez un SF2 General MIDI de bonne qualité et dont
  la licence autorise votre vidéo (par exemple piano, marimba, bell ou synth).
  Les changements de programme présents dans le MIDI sont respectés.
- `sounds/pass.wav` : WAV 44,1 kHz recommandé pour une latence faible. Vous
  pouvez le remplacer sans toucher au code, en gardant le même nom.

## Réglages principaux

Tous sont regroupés en haut de `main.py` :

- gravité : `GRAVITY` ;
- vitesse initiale/maximale : `BALL_SPEED`, `MAX_BALL_SPEED` ; chaque anneau
  franchi applique `SPEED_BOOST_PER_RING = 1.05`, soit une accélération de 5 % ;
- anneaux : `NUM_RINGS` (12–25), puis ajustez `RING_SPACING` pour que le dernier
  reste dans la largeur ;
- ouverture : `GAP_SIZE_DEGREES` ;
- rotation : `ROTATION_SPEED_MIN` et `ROTATION_SPEED_MAX` ;
- mode : `GAME_MODE = "classic"`, `"race"` ou `"prediction"` ;
- partage musical en course : `SHARED_MIDI` (`False` donne un index indépendant
  à chaque balle) ; chaque balle doit franchir elle-même chaque anneau ;
- rendu final direct : `SCALE = 1.0` pour une fenêtre 1080×1920 ;
- fluidité : `FPS = 60` ou `120`.

Commandes : `Espace` pause, `R` recommence, `Échap` quitte, `D` debug, `M` mute,
`F9` démarre/arrête l'enregistrement vidéo.

## Enregistrement intégré

Appuyez sur `F9` pour créer un MP4 vertical 1080×1920 à 60 FPS dans le dossier
`recordings/`. Appuyez de nouveau sur `F9` pour finaliser le fichier. Pour
enregistrer automatiquement dès le lancement, utilisez `RECORD_ON_START = True`.
La qualité se règle avec `RECORDING_CRF` (plus bas = meilleure qualité).

Le MP4 contient désormais **la vidéo H.264/yuv420p et l'audio AAC** : FluidSynth
reconstruit la musique depuis les événements de rebond enregistrés, puis le SFX
est mixé aux instants de passage. La timeline originale du MIDI ne pilote jamais
le morceau. Après F9 ou Échap, laissez la finalisation se terminer : le WAV et la
vidéo intermédiaire sont supprimés seulement après assemblage réussi. En cas
d'échec, ils restent disponibles et une erreur est affichée.

La cadence de capture est indépendante de `FPS` : un intervalle de 1/60 seconde
est simulé par image exportée. L'encodeur utilise une file bornée ; s'il est lent,
la prévisualisation attend, mais le MP4 ne perd aucune image et garde sa durée.
Le shake est exporté ; le panneau debug, la seed et le témoin REC restent dans
la fenêtre uniquement. La résolution exportée ne dépend pas de `SCALE`.

```powershell
python main.py --seed 89 --record
```

## Physique et architecture

`main.py` garde la configuration, les classes audio existantes, les états du jeu,
`ParticleSystem` et le rendu. `physics.py` contient `PhysicsEngine` et
`SimulationStats`. `recording_audio.py` synthétise et assemble la bande son.

La simulation accumule le temps et exécute des pas fixes de 1/240 seconde,
y compris la rotation des anneaux. La gravité est intégrée à chaque pas. Les
trajectoires sont linéaires à l'intérieur d'un pas : leurs intersections avec
les faces circulaires sont résolues analytiquement. Les caps arrondis mobiles
utilisent un avancement conservatif sur la distance avec une tolérance de
contact de 0,000001 pixel. La vitesse de rotation intervient dans leur rebond.
Le temps restant après un impact est consommé, autorisant plusieurs collisions.
Un epsilon de 0,0001 pixel assure la séparation numérique.

Un anneau disparaît seulement après dégagement de toute la balle au-delà de sa
face extérieure. La zone visible et les caps partagent la géométrie physique.
Chaque impact distinct joue un événement MIDI, même à moins de 60 ms du
précédent. `NOTE_COOLDOWN_MS` reste une ancienne valeur de référence ; il ne
filtre plus les vrais impacts. Les accords conservent la durée de chaque note.

## Seeds et recherche automatique

`SIMULATION_SEED = 89` est la seed choisie après recherche. Avec les réglages
actuels, elle termine en environ 33,15 secondes physiques, avec 68 rebonds et
8 near misses comptabilisés. Les ralentis de présentation peuvent allonger
légèrement le film. Les effets utilisent un générateur aléatoire distinct.
Même configuration et même seed reproduisent les états physiques au même tick
à 60 ou 120 FPS, sur cet environnement Python. La reproductibilité bit à bit
entre architectures différentes n'est pas garantie.

```powershell
python main.py --search 100 --seed 1
python main.py --seed 89
python main.py --mode race --seed 89
python main.py --mode prediction --seed 89
```

`AUTO_SEARCH_RUNS = 100` active aussi la recherche depuis la configuration.
La recherche n'ouvre pas de fenêtre, ne charge pas de SoundFont et n'émet aucun
son. Elle utilise le même moteur physique et écrit un rapport JSON horodaté dans
`recordings/`, avec passages et événements. Les dix meilleurs résultats sont
affichés en console. Le score tient compte de la durée cible (15–35 s), du plus
long silence d'événements, de la régularité des passages, des near misses et de
l'activité des quatre dernières secondes. Les séquences incomplètes sont
pénalisées. La recherche s'arrête après `SEARCH_TIMEOUT` ou une période sans
événement supérieure à `SEARCH_STALL_TIMEOUT`. Elle ne modifie aucune trajectoire.

`RANDOMIZE_SEED_ON_RESTART = False` rejoue la même seed ; `True` avance la seed
d'une unité à chaque partie. `SEAMLESS_LOOP = True` adoucit le redémarrage avec
un flash bref, sans écran de chargement (ce n'est pas une continuité géométrique
parfaite). `AUTO_RESTART` contrôle la relance ; `--no-restart` la désactive.

Les premières secondes affichent brièvement « CAN IT ESCAPE? ». L'anneau suivant
est mis en lumière. Les messages 3/2/1 restants sont temporaires. Les near misses
déclenchent un effet discret avec cooldown global. `SPEED_RAMP` applique un court
ralenti à toute la simulation (balles et anneaux ensemble), sans changer la
trajectoire en temps physique. Le payoff dure environ 0,85 s avec onde circulaire.

## Vérification

```powershell
python -m unittest discover -s tests -v
```

Les tests couvrent les intersections analytiques, les caps mobiles, le passage
complet, les impacts rapides, plusieurs impacts dans un pas, la concordance des
pixels/gaps, le MIDI multicanal et l'égalité exacte des états à 60/120 FPS.

## Enregistrement vertical dans OBS

1. Dans **Paramètres > Vidéo**, mettez résolution de base et de sortie à
   `1080x1920`, 60 FPS.
2. Réglez `SCALE = 1.0`, relancez le jeu, puis ajoutez une source **Capture de
   fenêtre** ciblant `Neon MIDI Rings`.
3. Ajoutez **Capture de sortie audio** pour la sortie utilisée par FluidSynth et
   Pygame. Vérifiez que le vumètre bouge lors des rebonds et des passages.
4. Enregistrez en MKV (plus sûr en cas d'arrêt), puis utilisez
   **Fichier > Remuxer les enregistrements** vers MP4.
5. Utilisez H.264, 12–20 Mb/s, images clés toutes les 2 secondes et audio AAC
   48 kHz / 320 kb/s. Le MP4 obtenu est prêt pour Shorts, Reels et TikTok.
#   v i r a l b l  
 