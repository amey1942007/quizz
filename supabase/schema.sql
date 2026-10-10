-- Live quiz tables for a SHARED Supabase project.
-- Every object is prefixed quiz_. This script does not drop or alter anything else.
-- Safe to run more than once. Apply it in the Supabase SQL editor.
-- Anon can SELECT leaderboard rows only. Writes use the service role (it bypasses RLS).

create table if not exists public.quiz_games (
  id text primary key,
  pin text not null,
  phase text not null default 'lobby',
  question_count integer not null default 0,
  current_index integer not null default -1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.quiz_players (
  id text primary key,
  game_id text not null references public.quiz_games (id) on delete cascade,
  nickname text not null,
  score integer not null default 0,
  streak integer not null default 0,
  joined_at timestamptz not null default now(),
  unique (game_id, nickname)
);

create table if not exists public.quiz_answers (
  id text primary key,
  game_id text not null references public.quiz_games (id) on delete cascade,
  player_id text not null references public.quiz_players (id) on delete cascade,
  question_index integer not null,
  choice integer,
  correct boolean not null default false,
  time_ms integer,
  points integer not null default 0,
  created_at timestamptz not null default now(),
  unique (game_id, player_id, question_index)
);

create table if not exists public.quiz_scores (
  game_id text not null references public.quiz_games (id) on delete cascade,
  player_id text not null references public.quiz_players (id) on delete cascade,
  nickname text not null,
  score integer not null default 0,
  rank integer not null default 0,
  streak integer not null default 0,
  last_points integer not null default 0,
  updated_at timestamptz not null default now(),
  primary key (game_id, player_id)
);

create index if not exists quiz_players_game_idx on public.quiz_players (game_id);
create index if not exists quiz_answers_game_idx on public.quiz_answers (game_id, question_index);
create index if not exists quiz_scores_game_rank_idx on public.quiz_scores (game_id, rank);

-- Safe columns only. security_invoker makes the caller's RLS apply.
create or replace view public.quiz_leaderboard
with (security_invoker = true) as
select
  game_id,
  player_id,
  nickname,
  score,
  rank,
  streak,
  last_points,
  updated_at
from public.quiz_scores;

alter table public.quiz_games enable row level security;
alter table public.quiz_players enable row level security;
alter table public.quiz_answers enable row level security;
alter table public.quiz_scores enable row level security;

revoke all on table public.quiz_games from public, anon, authenticated;
revoke all on table public.quiz_players from public, anon, authenticated;
revoke all on table public.quiz_answers from public, anon, authenticated;
revoke all on table public.quiz_scores from public, anon, authenticated;
revoke all on table public.quiz_leaderboard from public, anon, authenticated;

grant select on table public.quiz_scores to anon, authenticated;
grant select on table public.quiz_leaderboard to anon, authenticated;

drop policy if exists quiz_scores_anon_select on public.quiz_scores;
create policy quiz_scores_anon_select
on public.quiz_scores
for select
to anon, authenticated
using (true);

-- No insert, update, or delete policies: anon cannot write.

alter table public.quiz_games replica identity full;
alter table public.quiz_players replica identity full;
alter table public.quiz_answers replica identity full;
alter table public.quiz_scores replica identity full;

do $$
declare
  t text;
begin
  foreach t in array array['quiz_games', 'quiz_players', 'quiz_answers', 'quiz_scores']
  loop
    if not exists (
      select 1
      from pg_publication_tables
      where pubname = 'supabase_realtime'
        and schemaname = 'public'
        and tablename = t
    ) then
      execute format('alter publication supabase_realtime add table public.%I', t);
    end if;
  end loop;
end $$;
