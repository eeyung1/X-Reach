//! Lightweight scoring primitives based on the public X recommendation source.
//!
//! This crate accepts predicted model outputs. It does not infer propensities
//! from raw engagement counts and does not reproduce X's private production
//! models or personalized ranking pipeline.

/// Default Home Mixer weights from `home-mixer/params/param.rs` in
/// `x-algorithm-main`; author diversity defaults come from `vm-ranker/params.rs`.
#[derive(Clone, Debug, PartialEq)]
pub struct Weights {
    pub favorite: f64,
    pub reply: f64,
    pub bidirectional_follow_reply_boost: f64,
    pub retweet: f64,
    pub photo_expand: f64,
    pub video_open: f64,
    pub click: f64,
    pub open_link: f64,
    pub share: f64,
    pub share_via_dm: f64,
    pub share_via_copy_link: f64,
    pub dwell: f64,
    pub quote: f64,
    pub follow_author: f64,
    pub cont_dwell_time: f64,
    pub cont_click_dwell_time: f64,
    pub not_interested: f64,
    pub block_author: f64,
    pub mute_author: f64,
    pub report: f64,
    pub not_dwelled: f64,
    pub author_diversity_decay: f64,
    pub author_diversity_floor: f64,
}

impl Default for Weights {
    fn default() -> Self {
        Self {
            favorite: 0.66,
            reply: 5.0,
            bidirectional_follow_reply_boost: 15.0,
            retweet: 1.0,
            photo_expand: 0.05,
            video_open: 0.02,
            click: 0.3,
            open_link: 0.2,
            share: 2.0,
            share_via_dm: 5.0,
            share_via_copy_link: 20.0,
            dwell: 0.05,
            quote: 5.0,
            follow_author: 4.0,
            cont_dwell_time: 0.006,
            cont_click_dwell_time: 0.4,
            not_interested: -52.272,
            block_author: -31.2,
            mute_author: -58.8,
            report: -234.0,
            not_dwelled: -0.02,
            author_diversity_decay: 0.5,
            author_diversity_floor: 0.25,
        }
    }
}

/// Predicted action scores for one candidate. `dwell_seconds` and
/// `click_dwell_seconds` are continuous outputs; all other values are model
/// propensities. Missing heads contribute zero.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Predictions {
    pub favorite: Option<f64>,
    pub reply: Option<f64>,
    pub retweet: Option<f64>,
    pub photo_expand: Option<f64>,
    pub video_open: Option<f64>,
    pub click: Option<f64>,
    pub open_link: Option<f64>,
    pub share: Option<f64>,
    pub share_via_dm: Option<f64>,
    pub share_via_copy_link: Option<f64>,
    pub dwell: Option<f64>,
    pub quote: Option<f64>,
    pub follow_author: Option<f64>,
    pub dwell_seconds: Option<f64>,
    pub click_dwell_seconds: Option<f64>,
    pub not_interested: Option<f64>,
    pub block_author: Option<f64>,
    pub mute_author: Option<f64>,
    pub report: Option<f64>,
    pub not_dwelled: Option<f64>,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Candidate {
    pub author_id: u64,
    pub predictions: Predictions,
    pub is_reply: bool,
    pub is_retweet: bool,
    pub is_mutual_follow_author: bool,
    /// `Some(false)` applies the OON multiplier; `None` leaves it unapplied.
    pub in_network: Option<bool>,
    /// Optional cold-start target rank supplied by upstream eligibility logic.
    pub cold_start_lift_to_rank: Option<usize>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Score {
    /// Raw weighted propensity sum before the Phoenix negative-score offset.
    pub weighted: f64,
    pub final_score: f64,
    pub author_diversity_multiplier: f64,
    pub out_of_network_multiplier: f64,
    pub dropped_by: Vec<DropReason>,
}

/// Calibration parameters mapping final Phoenix scores to a percentile.
/// Empirical samples should be scores from posts with observed reach outcomes;
/// the percentile describes relative reach potential within that reference set.
#[derive(Clone, Debug, PartialEq)]
pub enum ReachCalibration {
    Empirical { reference_scores: Vec<f64> },
    LogNormal { log_mean: f64, log_std_dev: f64 },
}

impl ReachCalibration {
    /// Build a rank calibration from historical scores, requiring at least two
    /// finite reference observations.
    pub fn empirical(reference_scores: &[f64]) -> Result<Self, &'static str> {
        if reference_scores.len() < 2 || reference_scores.iter().any(|value| !value.is_finite()) {
            return Err("empirical calibration requires at least two finite scores");
        }
        let mut sorted = reference_scores.to_vec();
        sorted.sort_by(f64::total_cmp);
        Ok(Self::Empirical { reference_scores: sorted })
    }

    /// Fit a log-normal score distribution from historical positive scores.
    pub fn fit_log_normal(reference_scores: &[f64]) -> Result<Self, &'static str> {
        if reference_scores.len() < 2
            || reference_scores.iter().any(|value| !value.is_finite() || *value <= 0.0)
        {
            return Err("log-normal calibration requires at least two positive finite scores");
        }
        let logs: Vec<f64> = reference_scores.iter().map(|value| value.ln()).collect();
        let log_mean = logs.iter().sum::<f64>() / logs.len() as f64;
        let variance = logs.iter().map(|value| (value - log_mean).powi(2)).sum::<f64>()
            / logs.len() as f64;
        let log_std_dev = variance.sqrt();
        if !log_std_dev.is_finite() || log_std_dev <= 0.0 {
            return Err("log-normal calibration scores must have non-zero variance");
        }
        Ok(Self::LogNormal { log_mean, log_std_dev })
    }

    /// Return the calibrated percentile in the inclusive range 0 through 100.
    pub fn percentile(&self, score: f64) -> Result<f64, &'static str> {
        if !score.is_finite() {
            return Err("score must be finite");
        }
        match self {
            Self::Empirical { reference_scores } => {
                if reference_scores.len() < 2
                    || reference_scores.iter().any(|value| !value.is_finite())
                    || reference_scores.windows(2).any(|pair| pair[0] > pair[1])
                {
                    return Err("invalid empirical calibration");
                }
                let first = reference_scores.partition_point(|value| *value < score);
                let end = reference_scores.partition_point(|value| *value <= score);
                if first == end {
                    if first == 0 {
                        return Ok(0.0);
                    }
                    if first == reference_scores.len() {
                        return Ok(100.0);
                    }
                    let lower = first - 1;
                    let upper = first;
                    let fraction = (score - reference_scores[lower])
                        / (reference_scores[upper] - reference_scores[lower]);
                    let lower_rank = empirical_group_rank(reference_scores, reference_scores[lower]);
                    let upper_rank = empirical_group_rank(reference_scores, reference_scores[upper]);
                    let denominator = (reference_scores.len() - 1) as f64;
                    let lower_percentile = lower_rank / denominator;
                    let upper_percentile = upper_rank / denominator;
                    return Ok((lower_percentile + fraction * (upper_percentile - lower_percentile))
                        * 100.0);
                }
                let midrank = (first + end - 1) as f64 / 2.0;
                Ok((midrank / (reference_scores.len() - 1) as f64 * 100.0)
                    .clamp(0.0, 100.0))
            }
            Self::LogNormal { log_mean, log_std_dev } => {
                if !log_mean.is_finite() || !log_std_dev.is_finite() || *log_std_dev <= 0.0 {
                    return Err("invalid log-normal calibration");
                }
                if score <= 0.0 {
                    return Ok(0.0);
                }
                let z = (score.ln() - log_mean) / log_std_dev;
                Ok(normal_cdf(z) * 100.0)
            }
        }
    }

    pub fn method(&self) -> &'static str {
        match self {
            Self::Empirical { .. } => "empirical",
            Self::LogNormal { .. } => "log_normal",
        }
    }
}

fn empirical_group_rank(reference_scores: &[f64], value: f64) -> f64 {
    let first = reference_scores.partition_point(|sample| *sample < value);
    let end = reference_scores.partition_point(|sample| *sample <= value);
    (first + end - 1) as f64 / 2.0
}

// Abramowitz-Stegun 7.1.26 approximation; maximum absolute error is below
// 7.5e-8, sufficient for a user-facing percentile display.
fn normal_cdf(z: f64) -> f64 {
    if z <= -8.0 { return 0.0; }
    if z >= 8.0 { return 1.0; }
    let x = z.abs() / 2.0_f64.sqrt();
    let t = 1.0 / (1.0 + 0.3275911 * x);
    let erf = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t
        - 0.284496736) * t + 0.254829592) * t * (-x * x).exp();
    (0.5 * (1.0 + z.signum() * erf)).clamp(0.0, 1.0)
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SafetyLabel {
    SpamHighRecall,
    DoNotAmplify,
    NsfwHighRecall,
    MaliciousUrl,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum DropReason {
    SpamHighRecall,
    DoNotAmplify,
    NsfwHighRecall,
    MaliciousUrl,
}

/// Visibility-filtering flags available to the pre-flight emulator.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct VisibilitySignals {
    pub labels: Vec<SafetyLabel>,
    pub has_untrusted_external_url: bool,
}

/// Returns the drop reasons corresponding to the source visibility rules.
/// `NSFW_HIGH_RECALL` applies only when the candidate contains NSFW media;
/// callers set `has_nsfw_media` to model that condition.
pub fn visibility_drops(signals: &VisibilitySignals, has_nsfw_media: bool) -> Vec<DropReason> {
    let mut reasons = Vec::new();
    for label in &signals.labels {
        let reason = match label {
            SafetyLabel::SpamHighRecall => Some(DropReason::SpamHighRecall),
            SafetyLabel::DoNotAmplify => Some(DropReason::DoNotAmplify),
            SafetyLabel::NsfwHighRecall if has_nsfw_media => Some(DropReason::NsfwHighRecall),
            SafetyLabel::MaliciousUrl => Some(DropReason::MaliciousUrl),
            SafetyLabel::NsfwHighRecall => None,
        };
        if let Some(reason) = reason {
            if !reasons.contains(&reason) {
                reasons.push(reason);
            }
        }
    }
    if signals.has_untrusted_external_url && !reasons.contains(&DropReason::MaliciousUrl) {
        reasons.push(DropReason::MaliciousUrl);
    }
    reasons
}

/// Compute a raw weighted sum from predicted action propensities.
pub fn weighted_score(candidate: &Candidate, weights: &Weights) -> f64 {
    let p = &candidate.predictions;
    let reply_weight = weights.reply
        + if !candidate.is_reply && !candidate.is_retweet && candidate.is_mutual_follow_author {
            weights.bidirectional_follow_reply_boost
        } else {
            0.0
        };
    let terms = [
        (p.favorite, weights.favorite),
        (p.reply, reply_weight),
        (p.retweet, weights.retweet),
        (p.photo_expand, weights.photo_expand),
        (p.video_open, weights.video_open),
        (p.click, weights.click),
        (p.open_link, weights.open_link),
        (p.share, weights.share),
        (p.share_via_dm, weights.share_via_dm),
        (p.share_via_copy_link, weights.share_via_copy_link),
        (p.dwell, weights.dwell),
        (p.quote, weights.quote),
        (p.follow_author, weights.follow_author),
        (p.dwell_seconds, weights.cont_dwell_time),
        (p.click_dwell_seconds, weights.cont_click_dwell_time),
        (p.not_interested, weights.not_interested),
        (p.block_author, weights.block_author),
        (p.mute_author, weights.mute_author),
        (p.report, weights.report),
        (p.not_dwelled, weights.not_dwelled),
    ];
    terms
        .iter()
        .map(|(value, weight)| (*value).unwrap_or(0.0) * *weight)
        .sum()
}

/// Apply the small positive-score offset used by the public value-model code.
/// Negative weighted values are remapped into the range below that offset.
pub fn offset_score(weighted: f64, weights: &Weights) -> f64 {
    let positive_sum = weights.favorite
        + weights.reply
        + weights.retweet
        + weights.photo_expand
        + weights.video_open
        + weights.click
        + weights.open_link
        + weights.share
        + weights.share_via_dm
        + weights.share_via_copy_link
        + weights.dwell
        + weights.quote
        + weights.follow_author;
    let negative_sum = -(weights.not_interested
        + weights.block_author
        + weights.mute_author
        + weights.report
        + weights.not_dwelled);
    let total = positive_sum + negative_sum;
    if total == 0.0 {
        weighted.max(0.0)
    } else if weighted < 0.0 {
        (weighted + negative_sum) / total * 0.001
    } else {
        weighted + 0.001
    }
}

/// X's source formula: `(1 - floor) * decay^prior_posts + floor`.
pub fn author_diversity_multiplier(decay: f64, floor: f64, prior_posts: u32) -> f64 {
    (1.0 - floor) * decay.powf(f64::from(prior_posts)) + floor
}

/// Apply cold-start target-rank lifts, then score the candidate list with
/// author diversity in score order. A lift raises a candidate's score to at
/// least the score currently occupying its target rank.
pub fn score_candidates(
    candidates: &[Candidate],
    weights: &Weights,
    oon_multiplier: f64,
    visibility: &[VisibilitySignals],
    nsfw_media: &[bool],
) -> Vec<Score> {
    let base_weighted: Vec<f64> = candidates.iter().map(|c| weighted_score(c, weights)).collect();
    let mut weighted: Vec<f64> = base_weighted
        .iter()
        .map(|score| offset_score(*score, weights))
        .collect();

    for (index, candidate) in candidates.iter().enumerate() {
        if let Some(rank) = candidate.cold_start_lift_to_rank {
            if rank < weighted.len() {
                let mut sorted: Vec<f64> = base_weighted
                    .iter()
                    .map(|score| offset_score(*score, weights))
                    .collect();
                sorted.sort_by(|a, b| b.total_cmp(a));
                weighted[index] = weighted[index].max(sorted[rank]);
            }
        }
    }

    let mut order: Vec<usize> = (0..candidates.len()).collect();
    order.sort_by(|&a, &b| weighted[b].total_cmp(&weighted[a]));
    let mut output: Vec<Score> = candidates
        .iter()
        .enumerate()
        .map(|(i, candidate)| {
            let oon = if candidate.in_network == Some(false) {
                oon_multiplier
            } else {
                1.0
            };
            let drops = visibility
                .get(i)
                .map(|signals| visibility_drops(signals, *nsfw_media.get(i).unwrap_or(&false)))
                .unwrap_or_default();
            Score {
                weighted: weighted[i],
                final_score: if drops.is_empty() {
                    weighted[i] * oon
                } else {
                    0.0
                },
                author_diversity_multiplier: 1.0,
                out_of_network_multiplier: oon,
                dropped_by: drops,
            }
        })
        .collect();

    // Diversification order influences which of an author's candidates gets
    // the least-decayed multiplier, matching the source's rank-ordered count.
    for (rank, index) in order.iter().enumerate() {
        let candidate = &candidates[*index];
        let prior = order[..rank]
            .iter()
            .filter(|&&earlier| candidates[earlier].author_id == candidate.author_id)
            .count() as u32;
        output[*index].author_diversity_multiplier = author_diversity_multiplier(
            weights.author_diversity_decay,
            weights.author_diversity_floor,
            prior,
        );
        if output[*index].dropped_by.is_empty() {
            output[*index].final_score = weighted[*index]
                * output[*index].author_diversity_multiplier
                * output[*index].out_of_network_multiplier;
        }
    }
    output
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_match_public_home_mixer_weights() {
        let weights = Weights::default();
        assert_eq!(weights.share_via_copy_link, 20.0);
        assert_eq!(weights.favorite, 0.66);
        assert_eq!(weights.report, -234.0);
        assert_eq!(weights.author_diversity_decay, 0.5);
        assert_eq!(weights.author_diversity_floor, 0.25);
    }

    #[test]
    fn scoring_uses_propensities_and_mutual_reply_boost() {
        let candidate = Candidate {
            is_mutual_follow_author: true,
            predictions: Predictions { reply: Some(0.2), share_via_copy_link: Some(0.1), ..Default::default() },
            ..Default::default()
        };
        assert!((weighted_score(&candidate, &Weights::default()) - 6.0).abs() < 1e-12);
    }

    #[test]
    fn diversity_has_a_floor_and_decays_by_prior_rank() {
        assert_eq!(author_diversity_multiplier(0.5, 0.25, 0), 1.0);
        assert_eq!(author_diversity_multiplier(0.5, 0.25, 1), 0.625);
        assert_eq!(author_diversity_multiplier(0.5, 0.25, 20), 0.25 + 0.75 * 0.5_f64.powi(20));
    }

    #[test]
    fn cold_start_lift_raises_score_to_requested_rank() {
        let candidates = vec![
            Candidate { predictions: Predictions { favorite: Some(0.9), ..Default::default() }, ..Default::default() },
            Candidate { author_id: 2, cold_start_lift_to_rank: Some(0), predictions: Predictions { favorite: Some(0.1), ..Default::default() }, ..Default::default() },
        ];
        let scores = score_candidates(&candidates, &Weights::default(), 1.0, &[], &[]);
        assert!(scores[1].weighted >= scores[0].weighted);
    }

    #[test]
    fn visibility_labels_drop_candidates_and_unlabeled_nsfw_is_conditional() {
        let signals = VisibilitySignals { labels: vec![SafetyLabel::NsfwHighRecall, SafetyLabel::SpamHighRecall], has_untrusted_external_url: false };
        assert_eq!(visibility_drops(&signals, false), vec![DropReason::SpamHighRecall]);
        assert_eq!(visibility_drops(&signals, true), vec![DropReason::NsfwHighRecall, DropReason::SpamHighRecall]);
    }

    #[test]
    fn dropped_candidate_has_zero_final_score() {
        let signals = [VisibilitySignals { labels: vec![SafetyLabel::DoNotAmplify], ..Default::default() }];
        let scores = score_candidates(&[Candidate::default()], &Weights::default(), 0.5, &signals, &[false]);
        assert_eq!(scores[0].final_score, 0.0);
        assert_eq!(scores[0].dropped_by, vec![DropReason::DoNotAmplify]);
    }

    #[test]
    fn empirical_calibration_maps_scores_to_sample_percentile_ranks() {
        let calibration = ReachCalibration::empirical(&[3.0, 1.0, 2.0, 2.0]).unwrap();
        assert_eq!(calibration.method(), "empirical");
        assert_eq!(calibration.percentile(0.0).unwrap(), 0.0);
        assert_eq!(calibration.percentile(2.0).unwrap(), 50.0);
        assert_eq!(calibration.percentile(2.5).unwrap(), 75.0);
        assert_eq!(calibration.percentile(3.0).unwrap(), 100.0);
        assert_eq!(calibration.percentile(4.0).unwrap(), 100.0);
    }

    #[test]
    fn log_normal_calibration_uses_the_fitted_score_distribution() {
        let calibration = ReachCalibration::fit_log_normal(&[1.0, 4.0]).unwrap();
        assert_eq!(calibration.method(), "log_normal");
        assert!((calibration.percentile(2.0).unwrap() - 50.0).abs() < 0.0001);
        assert_eq!(calibration.percentile(0.0).unwrap(), 0.0);
        assert!(calibration.percentile(f64::NAN).is_err());
    }

    #[test]
    fn calibration_rejects_insufficient_or_invalid_reference_data() {
        assert!(ReachCalibration::empirical(&[1.0]).is_err());
        assert!(ReachCalibration::empirical(&[1.0, f64::INFINITY]).is_err());
        assert!(ReachCalibration::fit_log_normal(&[0.0, 2.0]).is_err());
        assert!(ReachCalibration::fit_log_normal(&[2.0, 2.0]).is_err());
    }
}
