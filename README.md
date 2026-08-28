# grafana-keycloak-group-syncer

Keycloak 그룹 멤버십을 Grafana OSS 팀 멤버십에 주기적으로 반영하는 멱등 동기화 잡입니다.
Grafana Enterprise 의 Team Sync 를 대체하며, Kubernetes CronJob 으로 10분마다 실행합니다.

## 그룹 구조

`GROUP_PREFIX`(예: `service`)로 시작하는 그룹이 **루트 그룹**이고,
루트의 **1뎁스 하위가 서비스(팀명)**, 그 **다음 뎁스가 역할 그룹**입니다.
역할 그룹은 각각 **독립 Grafana 팀**으로 동기화됩니다.

```
service                 ← GROUP_PREFIX 와 매칭되는 루트 그룹
└── abc                 → (직속 멤버가 있으면) Grafana 팀 "abc"
    ├── abc_adm         → Grafana 팀 "abc_adm"
    ├── abc_editor      → Grafana 팀 "abc_editor"
    └── abc_viewer      → Grafana 팀 "abc_viewer"
```

- **Admin/Editor/Viewer 권한 부여는 폴더 권한 단계에서** 이뤄집니다.
  Grafana 팀 멤버십 자체에는 Editor/Viewer 개념이 없으므로, 이 도구는 역할별
  팀만 만들어 주고, 폴더 ↔ 팀 권한(Admin/Edit/View)은 별도 작업(Terraform
  또는 수동)에서 `abc_adm`→Admin, `abc_editor`→Edit, `abc_viewer`→View 로
  부여합니다.
- 인식하는 역할 suffix 는 `ROLE_SUFFIXES` 환경변수로 정의합니다 (기본값
  `adm,admin,editor,viewer,member,mbr`). 역할 그룹 이름은
  `<서비스명>_<suffix>` 또는 `<서비스명>-<suffix>` 형태여야 하며(대소문자
  무관), 그 외 이름의 하위 그룹은 경고 로그를 남기고 건너뜁니다.
- 서비스 그룹의 직속 멤버는 서비스명과 같은 이름의 팀으로 동기화됩니다.
  직속 멤버가 없고 같은 이름의 팀도 아직 없으면 빈 팀을 만들지 않습니다.
- org 롤(Viewer/Editor/Admin)은 기존대로 `role_attribute_path` 가 담당합니다.
  이 도구는 팀 멤버십만 책임집니다.

## 동작 방식

1. Keycloak 그룹 트리에서 prefix 와 매칭되는 루트 그룹을 찾고, 그 하위
   서비스 그룹의 역할 그룹들을 팀 목록으로 만듭니다. 팀 이름은 역할 그룹
   이름 그대로입니다 (예: `abc_adm`).
   - Keycloak 23+ 의 `/groups/{id}/children` 엔드포인트를 사용하고,
     구버전에서는 `subGroups` 필드로 자동 fallback 합니다.
2. 각 그룹의 직속 멤버(비활성 사용자 제외)를 `MATCH_KEY`(`email` 기본 또는
   `username`)로 Grafana 사용자와 매칭합니다. 비교는 소문자 정규화 후
   수행합니다.
3. Grafana 에 팀이 없으면 생성하고, 현재 팀 멤버와 비교해 추가/제거합니다.
   - 아직 Grafana 에 로그인한 적 없는 사용자(lookup 404)는 건너뛰고 pending 으로
     집계하며, 다음 주기에 자동 재시도됩니다.
   - prefix 밖의 팀은 조회조차 하지 않으므로 절대 변경되지 않습니다.

## 예제

Keycloak 그룹이 다음과 같다고 하겠습니다.

```
service
├── abc
│   ├── abc_adm          멤버: alice@example.com
│   ├── abc_editor       멤버: bob@example.com
│   └── abc_viewer       멤버: carol@example.com
├── xyz                  직속 멤버: dave@example.com
│   └── xyz_viewer       멤버: erin@example.com
└── legacy
    └── legacy_ops       ← suffix 미인식: 경고 후 스킵
hr                       ← prefix 미매칭: 아예 조회하지 않음
└── payroll
```

설정:

```sh
GROUP_PREFIX=service
ROLE_SUFFIXES="adm,editor,viewer"
MATCH_KEY=email
DRY_RUN=false
```

동기화 결과 생성되는 Grafana 팀:

| Grafana 팀 | 멤버 | 이후 폴더 권한 부여 (Terraform, 범위 밖) |
|---|---|---|
| `abc_adm` | alice@example.com | `abc` 폴더에 **Admin** |
| `abc_editor` | bob@example.com | `abc` 폴더에 **Edit** |
| `abc_viewer` | carol@example.com | `abc` 폴더에 **View** |
| `xyz` | dave@example.com | (서비스 그룹 직속 멤버) |
| `xyz_viewer` | erin@example.com | `xyz` 폴더에 **View** |

- `legacy_ops` 는 `ROLE_SUFFIXES` 에 없는 suffix 라 경고 후 스킵되고,
  `legacy` 는 직속 멤버가 없으므로 빈 팀을 만들지 않습니다.
- `hr`/`payroll` 은 prefix 밖이므로 팀이 생성되지 않고, 같은 이름의 기존
  Grafana 팀이 있어도 건드리지 않습니다.
- carol 이 아직 Grafana 에 로그인한 적이 없다면 이번 주기에는
  `member_pending_first_login` 으로 건너뛰고, 최초 로그인 후 다음 주기에
  자동으로 팀에 편입됩니다.
- 이후 Keycloak 에서 bob 을 `abc_editor` 에서 빼면 다음 주기에 팀
  `abc_editor` 에서도 제거되고, alice 를 `abc_adm` 에서 `abc_viewer` 로
  옮기면 두 팀의 멤버십이 그에 맞게 갱신됩니다.

`DRY_RUN=true` 로 먼저 실행하면 위 변경이 다음과 같은 로그로만 출력됩니다.

```
time=... level=INFO event=dry_run_enabled
time=... level=WARNING event=unknown_role_group_skipped service=legacy group=legacy_ops expected=legacy_adm|legacy_editor|legacy_viewer
time=... level=INFO event=would_create_team team=abc_adm
time=... level=INFO event=would_add_member team=abc_adm target=alice@example.com
time=... level=INFO event=would_create_team team=abc_editor
time=... level=INFO event=would_add_member team=abc_editor target=bob@example.com
time=... level=INFO event=would_create_team team=abc_viewer
time=... level=INFO event=member_pending_first_login team=abc_viewer target=carol@example.com
time=... level=INFO event=empty_team_not_created team=legacy
... (xyz / xyz_viewer 생략)
time=... level=INFO event=sync_complete teams=7 failed_teams=0 added=4 removed=0 pending_first_login=1 dry_run=True exit_code=0
```

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
| `ROLE_SUFFIXES` | N | `adm,admin,editor,viewer,member,mbr` | 인식할 역할 그룹 suffix 목록 (콤마 구분) |
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
time=2026-08-27T09:00:01+0000 level=INFO event=add_member team=abc_editor target=alice@example.com
time=2026-08-27T09:00:01+0000 level=INFO event=remove_member team=abc_viewer target=bob@example.com
time=2026-08-27T09:00:02+0000 level=INFO event=sync_complete teams=6 failed_teams=0 added=1 removed=1 pending_first_login=0 dry_run=False exit_code=0
```

## 개발

```sh
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

테스트는 `responses` 로 Keycloak/Grafana API 를 모킹하며, 팀 생성·멤버 추가/제거,
역할 그룹의 독립 팀 생성, suffix 인식/스킵, 페이지네이션, 제거 가드,
dry-run 무변경, 토큰 재발급 등을 커버합니다.
