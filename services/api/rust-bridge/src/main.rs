use serde::{Deserialize, Serialize};
use std::io::{self, Read};
use xreach_core::{
    score_candidates, Candidate, DropReason, Predictions, SafetyLabel, ScoreCalibration,
    VisibilitySignals, Weights,
};

const PERCENTILE_CONFIDENCE_LEVEL: f64 = 0.95;

#[derive(Deserialize)]
struct ScoreRequest {
    #[serde(default, alias = "reach_calibration")]
    score_calibration: Option<ScoreCalibrationInput>,
    #[serde(default)]
    predictions: PredictionInput,
    #[serde(default)]
    author_id: u64,
    #[serde(default)]
    is_reply: bool,
    #[serde(default)]
    is_retweet: bool,
    #[serde(default)]
    is_mutual_follow_author: bool,
    #[serde(default)]
    in_network: Option<bool>,
    #[serde(default)]
    cold_start_lift_to_rank: Option<usize>,
    #[serde(default)]
    visibility_labels: Vec<String>,
    #[serde(default)]
    has_nsfw_media: bool,
    #[serde(default)]
    has_untrusted_external_url: bool,
    #[serde(default = "one")]
    oon_multiplier: f64,
}

#[derive(Deserialize)]
#[serde(tag = "method", rename_all = "snake_case")]
enum ScoreCalibrationInput {
    Empirical { reference_scores: Vec<f64> },
    LogNormal { log_mean: f64, log_std_dev: f64 },
}

impl TryFrom<ScoreCalibrationInput> for ScoreCalibration {
    type Error = &'static str;

    fn try_from(input: ScoreCalibrationInput) -> Result<Self, Self::Error> {
        match input {
            ScoreCalibrationInput::Empirical { reference_scores } => {
                ScoreCalibration::empirical(&reference_scores)
            }
            ScoreCalibrationInput::LogNormal {
                log_mean,
                log_std_dev,
            } => {
                if !log_mean.is_finite() || !log_std_dev.is_finite() || log_std_dev <= 0.0 {
                    return Err(
                        "log-normal parameters must be finite and standard deviation positive",
                    );
                }
                Ok(ScoreCalibration::LogNormal {
                    log_mean,
                    log_std_dev,
                })
            }
        }
    }
}

#[derive(Default, Deserialize)]
struct PredictionInput {
    favorite: Option<f64>,
    reply: Option<f64>,
    retweet: Option<f64>,
    photo_expand: Option<f64>,
    video_open: Option<f64>,
    click: Option<f64>,
    open_link: Option<f64>,
    share: Option<f64>,
    share_via_dm: Option<f64>,
    share_via_copy_link: Option<f64>,
    dwell: Option<f64>,
    quote: Option<f64>,
    follow_author: Option<f64>,
    dwell_seconds: Option<f64>,
    click_dwell_seconds: Option<f64>,
    not_interested: Option<f64>,
    block_author: Option<f64>,
    mute_author: Option<f64>,
    report: Option<f64>,
    not_dwelled: Option<f64>,
}

impl From<PredictionInput> for Predictions {
    fn from(input: PredictionInput) -> Self {
        Self {
            favorite: input.favorite,
            reply: input.reply,
            retweet: input.retweet,
            photo_expand: input.photo_expand,
            video_open: input.video_open,
            click: input.click,
            open_link: input.open_link,
            share: input.share,
            share_via_dm: input.share_via_dm,
            share_via_copy_link: input.share_via_copy_link,
            dwell: input.dwell,
            quote: input.quote,
            follow_author: input.follow_author,
            dwell_seconds: input.dwell_seconds,
            click_dwell_seconds: input.click_dwell_seconds,
            not_interested: input.not_interested,
            block_author: input.block_author,
            mute_author: input.mute_author,
            report: input.report,
            not_dwelled: input.not_dwelled,
        }
    }
}

#[derive(Serialize)]
struct PercentileBounds {
    lower: f64,
    upper: f64,
    confidence_level: f64,
    method: &'static str,
}

#[derive(Serialize)]
struct ScoreResponse {
    weighted_score: f64,
    final_score: f64,
    author_diversity_multiplier: f64,
    out_of_network_multiplier: f64,
    dropped_by: Vec<&'static str>,
    model_score_percentile: Option<f64>,
    model_score_percentile_bounds: Option<PercentileBounds>,
    score_calibration_status: &'static str,
}

fn one() -> f64 {
    1.0
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let mut raw = String::new();
    io::stdin().read_to_string(&mut raw)?;
    let request: ScoreRequest = serde_json::from_str(&raw)?;
    let candidate = Candidate {
        author_id: request.author_id,
        predictions: request.predictions.into(),
        is_reply: request.is_reply,
        is_retweet: request.is_retweet,
        is_mutual_follow_author: request.is_mutual_follow_author,
        in_network: request.in_network,
        cold_start_lift_to_rank: request.cold_start_lift_to_rank,
    };
    let visibility = VisibilitySignals {
        labels: request
            .visibility_labels
            .iter()
            .filter_map(|label| match label.as_str() {
                "spam_high_recall" => Some(SafetyLabel::SpamHighRecall),
                "do_not_amplify" => Some(SafetyLabel::DoNotAmplify),
                "nsfw_high_recall" => Some(SafetyLabel::NsfwHighRecall),
                "malicious_url" => Some(SafetyLabel::MaliciousUrl),
                _ => None,
            })
            .collect(),
        has_untrusted_external_url: request.has_untrusted_external_url,
    };
    if !request.oon_multiplier.is_finite() || request.oon_multiplier < 0.0 {
        return Err("oon_multiplier must be a finite non-negative number".into());
    }
    let scores = score_candidates(
        &[candidate],
        &Weights::default(),
        request.oon_multiplier,
        &[visibility],
        &[request.has_nsfw_media],
    );
    let score = scores.first().ok_or("scorer returned no candidate")?;
    let calibration = request
        .score_calibration
        .map(ScoreCalibration::try_from)
        .transpose()?;
    let model_score_percentile = calibration
        .as_ref()
        .map(|model| model.percentile(score.final_score))
        .transpose()?;
    let model_score_percentile_bounds = calibration
        .as_ref()
        .map(|model| model.percentile_bounds(score.final_score, PERCENTILE_CONFIDENCE_LEVEL))
        .transpose()?
        .flatten()
        .map(|(lower, upper)| PercentileBounds {
            lower,
            upper,
            confidence_level: PERCENTILE_CONFIDENCE_LEVEL,
            method: "dkw",
        });
    let response = ScoreResponse {
        weighted_score: score.weighted,
        final_score: score.final_score,
        author_diversity_multiplier: score.author_diversity_multiplier,
        out_of_network_multiplier: score.out_of_network_multiplier,
        dropped_by: score.dropped_by.iter().map(drop_name).collect(),
        model_score_percentile,
        model_score_percentile_bounds,
        score_calibration_status: calibration
            .as_ref()
            .map(ScoreCalibration::method)
            .unwrap_or("unavailable"),
    };
    println!("{}", serde_json::to_string(&response)?);
    Ok(())
}

fn drop_name(reason: &DropReason) -> &'static str {
    match reason {
        DropReason::SpamHighRecall => "spam_high_recall",
        DropReason::DoNotAmplify => "do_not_amplify",
        DropReason::NsfwHighRecall => "nsfw_high_recall",
        DropReason::MaliciousUrl => "malicious_url",
    }
}

fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(2);
    }
}
