# grafana-keycloak-group-syncer

Keycloak 그룹 멤버십을 Grafana OSS 팀 멤버십에 주기적으로 반영하는 멱등 동기화 잡입니다.
Grafana Enterprise 의 Team Sync 를 대체하며, Kubernetes CronJob 으로 10분마다 실행합니다.

## 그룹 구조

`GROUP_PREFIX`(예: `service`)로 시작하는 그룹이 **루트 그룹**이고,
루트의 **1뎁스 하위가 팀**, 그 **다음 뎁스가 권한 그룹**입니다.

```
service                 ← GROUP_PREFIX 와 매칭되는 루트 그룹
└── abc                 → Grafana 팀 "abc" (직속 멤버는 Member 권한)
    ├── abc_member      → 팀 "abc" 의 Member
    └── abc_adm         → 팀 "abc" 의 Admin (팀 관리자)
```

- 권한 그룹 이름은 `<팀명>_adm`, `<팀명>_admin`, `<팀명>_member`, `<팀명>_mbr`
  형태를 인식합니다 (`_` 대신 `-` 도 허용, 팀명 없이 `admin`/`member` 도 허용,
  대소문자 무관). 그 외 이름의 하위 그룹은 경고 로그를 남기고 건너뜁니다.
- 팀 그룹의 직속 멤버는 Member 로 취급합니다. 한 사용자가 member 와 admin
  양쪽에 있으면 Admin 이 우선합니다.
- 여기서 말하는 권한은 Grafana **팀 멤버 권한**(Member/Admin)입니다.
  org 롤(Viewer/Editor/Admin)은 기존대로 `role_attribute_path` 가 담당합니다.

## 동작 방식

1. Keycloak 그룹 트리에서 prefix 와 매칭되는 루트 그룹을 찾고, 그 하위
   그룹(1뎁스)을 팀으로 사용합니다. 팀 이름은 하위 그룹 이름 그대로입니다.
   - Keycloak 23+ 의 `/groups/{id}/children` 엔드포인트를 사용하고,
     구버전에서는 `subGroups` 필드로 자동 fallback 합니다.
2. 팀 그룹의 직속 멤버와 권한 하위 그룹 멤버(비활성 사용자 제외)를
   `MATCH_KEY`(`email` 기본 또는 `username`)로 Grafana 사용자와 매칭합니다.
   비교는 소문자 정규화 후 수행합니다.
3. Grafana 에 팀이 없으면 생성하고, 현재 팀 멤버와 비교해 추가/제거하며,
   권한이 다른 기존 멤버는 권한을 갱신합니다.
   - 아직 Grafana 에 로그인한 적 없는 사용자(lookup 404)는 건너뛰고 pending 으로
     집계하며, 다음 주기에 자동 재시도됩니다.
   - prefix 밖의 팀은 조회조차 하지 않으므로 절대 변경되지 않습니다.

### 범위 밖 (별도 작업)

- 폴더 ↔ 팀 권한 부여, org 롤 매핑(`role_attribute_path`), grafana.ini 변경
- **팀 삭제**: Keycloak 에서 그룹이 사라져도 Grafana 팀은 남습니다. 이 잡은 남은
  팀을 조회하지 않으므로(관리 대상 팀 이름으로만 조회) 고아 팀 정리는 수동으로 합니다.

## 안전장치

- `DRY_RUN` 기본값 **true** — 실제 변경 없이 예상 diff(`would_create_team`,
  `would_add_member`, `would_remove_member`)만 로그로 출력합니다.
  배포 매니페스트에서 명시적으로 `false` 를 줘야 실제 반영됩니다.
- 한 팀에서 제거 대상이 현재 멤버의 `MAX_REMOVAL_RATIO`(기본 0.5)를 **초과**하면
  해당 팀의 제거를 스킵하고(추가는 수행) 에러 로그를 남기며 종료 코드 1 로 끝납니다.
  `MATCH_KEY` 오설정으로 전원이 제거되는 사고를 막기 위한 장치입니다.
- 관리 대상 그룹이 0개면 경고만 남기고 아무것도 변경하지 않습니다.
- 모든 쓰기 작업은 실행 전에 로그로 남깁니다. 토큰·시크릿은 로그에 출력하지 않습니다.

## 종료 코드

| 코드 | 의미 |
|---|---|
| 0 | 정상 |
| 1 | 부분 실패 (제거 가드 발동, 일부 팀 처리 실패) |
| 2 | 설정 오류 또는 인증 실패 |

## 설정 (환경변수)

| 변수 | 필수 | 기본값 | 설명 |
|---|---|---|---|
| `KEYCLOAK_URL` | Y | | Keycloak base URL (예: `https://keycloak.example.com`) |
| `KEYCLOAK_REALM` | Y | | realm 이름 |
| `KEYCLOAK_CLIENT_ID` | Y | | service account 가 활성화된 confidential client |
| `KEYCLOAK_CLIENT_SECRET` | Y | | client secret — K8s Secret 으로 주입 |
| `GRAFANA_URL` | Y | | Grafana base URL |
| `GRAFANA_TOKEN` | Y | | 서비스 계정 토큰 — K8s Secret 으로 주입 |
| `GROUP_PREFIX` | N | `grafana-` | 루트 그룹 이름 prefix (예: `service`) |
| `MATCH_KEY` | N | `email` | `email` 또는 `username`. Grafana `login_attribute_path` 와 일치해야 함 |
| `DRY_RUN` | N | `true` | 변경 없이 로그만 출력 |
| `MAX_REMOVAL_RATIO` | N | `0.5` | 팀별 제거 가드 임계값 (0~1) |
| `LOG_LEVEL` | N | `INFO` | |

## 사전 준비

### 1. Keycloak client 설정

1. 대상 realm 에 confidential client 생성 (예: `grafana-team-sync`).
   - **Client authentication**: On
   - **Service accounts roles**: On (client_credentials grant)
   - Standard flow 등 나머지 flow 는 모두 Off
2. **Service accounts roles** 탭 → *Assign role* → *Filter by clients* 에서
   `realm-management` client 의 다음 롤을 부여:
   - `view-users`
   - `query-groups`
3. **Credentials** 탭의 client secret 을 K8s Secret 으로 보관합니다.

### 2. Grafana 서비스 계정 발급

1. Grafana → **Administration → Service accounts → Add service account**
   - Role: **Admin** (org admin — 팀 생성/멤버 관리와 `/api/users/lookup` 에 필요)
2. 생성한 서비스 계정에서 **Add service account token** 으로 토큰을 발급하고
   K8s Secret 으로 보관합니다.
3. `MATCH_KEY` 는 grafana.ini 의 OAuth `login_attribute_path` 설정과 일치해야
   합니다. login 이 Keycloak username 이면 `MATCH_KEY=username` 을 사용하세요.

### 3. 시크릿 생성

```sh
kubectl create secret generic grafana-team-sync \
  --from-literal=KEYCLOAK_CLIENT_SECRET='...' \
  --from-literal=GRAFANA_TOKEN='...'
```

매니페스트에 평문 시크릿을 넣지 마세요. `k8s/secret.example.yaml` 은 키 이름
참고용 예시입니다.

## 최초 배포 절차 (dry-run 먼저)

1. 이미지를 빌드해 레지스트리에 푸시합니다.

   ```sh
   docker build -t registry.example.com/grafana-team-sync:<tag> .
   docker push registry.example.com/grafana-team-sync:<tag>
   ```

2. `k8s/cronjob.yaml` 의 이미지/URL/realm 값을 채우고 **`DRY_RUN=true`** 로 배포합니다.
3. Job 로그에서 예상 diff 를 검토합니다.

   ```sh
   kubectl create job --from=cronjob/grafana-team-sync team-sync-dryrun
   kubectl logs job/team-sync-dryrun
   ```

   확인할 것:
   - `would_create_team` / `would_add_member` / `would_remove_member` 가 기대와 일치하는가
   - `member_pending_first_login` (아직 미로그인 사용자) 수가 타당한가
   - `removal_guard_triggered` 가 떴다면 `MATCH_KEY` 설정을 의심할 것
4. diff 가 정상이면 `DRY_RUN=false` 로 변경해 재배포합니다.
5. 두 번 연속 실행 후 두 번째 실행에서 `added=0 removed=0` 인지(멱등) 확인합니다.

## 로그 형식

한 줄 단위 logfmt 스타일 구조화 로그입니다.

```
time=2026-08-27T09:00:01+0000 level=INFO event=add_member team=devs target=alice@example.com permission=Member
time=2026-08-27T09:00:01+0000 level=INFO event=update_permission team=devs target=bob@example.com permission=Admin
time=2026-08-27T09:00:02+0000 level=INFO event=sync_complete teams=3 failed_teams=0 added=2 removed=1 permission_updates=1 pending_first_login=1 dry_run=False exit_code=0
```

## 개발

```sh
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

테스트는 `responses` 로 Keycloak/Grafana API 를 모킹하며, 팀 생성·멤버 추가/제거,
권한(admin/member) 하위 그룹과 권한 갱신, 페이지네이션, 제거 가드, dry-run 무변경,
토큰 재발급 등을 커버합니다.
