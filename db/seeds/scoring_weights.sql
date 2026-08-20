-- The scoring model, as data.
--
--   psql "$BIZDATA_DSN" -f db/seeds/scoring_weights.sql
--
-- scripts/seed.py applies this after the six generated tables are loaded, and before
-- db/views/engagement_health_v1.sql, which reads all three tables below.
--
-- Nothing here is generated and nothing here depends on --seed. These are reference
-- rows, which is why they live in db/seeds/ rather than in the generator and why they
-- stay out of db/checks/checksums.sql.
--
-- Two versions are in play and they version different things. The view name's _v1 is
-- the component shape: which four things are measured and how each raw value becomes a
-- risk. scoring_weights.version is the calibration: how those four are traded off
-- against each other, and where the band edges sit. Changing a weight is a new
-- calibration against the same view. Changing what a component measures is
-- engagement_health_v2, because the numbers stop being comparable month over month.
--
-- Re-runnable. Every statement is guarded, so applying this twice is a no-op rather
-- than an error.

-- --------------------------------------------------------------------------------------
-- Tables
-- --------------------------------------------------------------------------------------

create table if not exists scoring_model (
    version    text    not null primary key,
    is_active  boolean not null default false,
    notes      text
);

-- At most one active model at a time, enforced rather than agreed. The view joins on
-- is_active and a second active row would silently double every engagement's rows.
create unique index if not exists scoring_model_one_active
    on scoring_model (is_active)
    where is_active;

create table if not exists scoring_weights (
    version    text          not null,
    component  text          not null,
    weight     numeric(6,4)  not null check (weight >= 0),
    primary key (version, component),
    foreign key (version) references scoring_model (version)
);

-- Band edges are data for the same reason weights are: moving the amber line is a
-- calibration decision, and a calibration decision that requires a view change is one
-- nobody makes between releases.
create table if not exists scoring_bands (
    version    text         not null,
    band       text         not null check (band in ('green', 'amber', 'red')),
    min_score  numeric(5,2) not null check (min_score >= 0 and min_score <= 100),
    primary key (version, band),
    foreign key (version) references scoring_model (version)
);

-- --------------------------------------------------------------------------------------
-- Version v1.0
-- --------------------------------------------------------------------------------------

insert into scoring_model (version, is_active, notes)
values (
    'v1.0',
    true,
    'Four components against engagement_health_v1: burn trajectory, margin, reporting '
    'gap, payment behaviour. Weights reflect that the partner deck is a delivery and '
    'margin conversation first; reporting gaps matter because they decide whether the '
    'other numbers can be trusted, and payment behaviour is the client-side signal '
    'least within the delivery team''s control.'
)
on conflict (version) do update
    set is_active = excluded.is_active,
        notes     = excluded.notes;

-- Weights do not have to sum to 1. The view normalises over the sum of whichever
-- components it could measure for a given engagement, so these are relative rather than
-- absolute, and a single UPDATE to one row is a valid recalibration on its own.
insert into scoring_weights (version, component, weight) values
    ('v1.0', 'burn_trajectory',   0.35),
    ('v1.0', 'margin',            0.30),
    ('v1.0', 'reporting_gap',     0.20),
    ('v1.0', 'payment_behaviour', 0.15)
on conflict (version, component) do update
    set weight = excluded.weight;

-- Scores run 0 to 100 with higher being healthier, so these are floors: green at 70 and
-- above, amber from 40, red below that. Only the green and amber floors are read by the
-- view; red is stored so the model is legible from the table alone rather than requiring
-- someone to infer the bottom band from the absence of a row.
insert into scoring_bands (version, band, min_score) values
    ('v1.0', 'green', 70.00),
    ('v1.0', 'amber', 40.00),
    ('v1.0', 'red',    0.00)
on conflict (version, band) do update
    set min_score = excluded.min_score;
