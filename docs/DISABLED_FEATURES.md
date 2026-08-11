# Fonctionnalités désactivées

## Driver Monitoring (DM)

**Status :** Désactivé
**Raison :** Faux positifs causant des désengagements intempestifs du pilote automatique.

### Ce qui a été modifié

| Fichier | Modification |
|---------|-------------|
| `system/manager/process_config.py` | `dmonitoringmodeld` et `dmonitoringd` mis à `enabled=False` |
| `selfdrive/selfdrived/selfdrived.py` | `driverCameraState` retiré de `camera_packets`, `driverMonitoringState` ajouté à la liste `ignore`, events DM plus ajoutés |
| `selfdrive/controls/controlsd.py` | `forceDecel` ne dépend plus de `awarenessStatus` |
| `selfdrive/modeld/modeld.py` | `isRHD` fixé à `False` (plus de lecture depuis DM) |
| `selfdrive/monitoring/helpers.py` | `_update_events` neutralisé (reset awareness au lieu de générer des alertes) |

### Pour réactiver

1. Remettre `enabled=(WEBCAM or not PC)` sur `dmonitoringmodeld` et `dmonitoringd` dans `process_config.py`
2. Remettre `"driverCameraState"` dans `camera_packets` dans `selfdrived.py`
3. Retirer `'driverMonitoringState'` de la liste `ignore` dans `selfdrived.py`
4. Décommenter `self.events.add_from_msg(self.sm['driverMonitoringState'].events)` dans `selfdrived.py`
5. Restaurer `forceDecel` avec `awarenessStatus` dans `controlsd.py`
6. Restaurer `is_rhd = sm["driverMonitoringState"].isRHD` dans `modeld.py`
7. Restaurer la logique originale de `_update_events` dans `monitoring/helpers.py`

---

## Alerte ceinture de sécurité

**Status :** Désactivé
**Raison :** Faux positifs (`seatbeltNotLatched`) causant des désengagements non souhaités.

### Ce qui a été modifié

| Fichier | Modification |
|---------|-------------|
| `selfdrive/car/car_specific.py` | Event `seatbeltNotLatched` commenté (ne s'ajoute plus aux events) |

### Pour réactiver

Décommenter dans `selfdrive/car/car_specific.py` :
```python
if CS.seatbeltUnlatched:
    events.add(EventName.seatbeltNotLatched)
```
