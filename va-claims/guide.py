"""Federal-standards guide text, split out so both the agent tool and the
dashboard API read one source. Citations verified against eCFR/govinfo 2026-11."""

GUIDE = {
    "file": """Filing a VA disability claim (VA Form 21-526EZ)
The legal basis: 38 CFR 3.151 requires a specific claim on the form prescribed by the Secretary before any benefit is paid. 38 CFR 3.155 says how to file.
1. Intent to file (38 CFR 3.155(b)). If you need time for evidence, submit an intent to file first. It must identify the general benefit (compensation) and identify you, but it does NOT need to name conditions. Valid routes: a saved electronic application on va.gov, a signed written intent on the prescribed form, or an oral intent recorded by a VA employee. A complete claim filed within 1 year of the intent is treated as filed on the intent date.
2. File the claim: va.gov (Login.gov, ID.me, or DS Logon), by mail to the Claims Intake Center, PO Box 4444, Janesville, WI 53547-4444, or through an accredited representative.
3. List each condition. VA calls these contentions. Match official names with va_disability_lookup.
4. For each condition VA needs three things (the service-connection triangle): an in-service injury, event, or aggravation; a current diagnosed disability; and a link (nexus) between them.
5. Consider a Fully Developed Claim: submit 21-526EZ plus every piece of evidence you hold, certify you have no more evidence, and attend any VA exam scheduled. VA processes FDC claims faster. If you add evidence later, the claim drops to standard processing.
6. Watch for the Development Letter (the duty-to-assist notice under 38 CFR 3.159). You have 1 year to respond, but VA may decide the claim on the file as-is if you have not responded after 30 days. Respond fast with whatever you have.
7. Track with va_claim_track or at va.gov under Check Claims Status.
Common mistakes: claiming conditions with no current diagnosis, vague descriptions with no in-service event, and skipping lay statements when service treatment records are thin or destroyed (the 1973 NPRC fire burned many Army records; VA applies a relaxed evidentiary standard for those).""",
    "evidence": """What must be proven, and with what
The three elements (38 CFR 3.303 and VA case law): (1) an in-service incurrence or aggravation, (2) a current diagnosis, (3) a nexus between them.
The standard of proof is low. VA decides on "at least as likely as not", a 50 percent threshold, not "more likely than not". When positive and negative evidence are in approximate balance, the benefit of the doubt goes to the veteran (38 USC 5107(b), 38 CFR 3.102, 38 CFR 4.3). A nexus opinion saying "possible" falls below the bar; "at least as likely as not (50% or greater probability)" with stated medical reasoning is what wins.
Evidence that carries weight:
- Service treatment records (STRs). VA obtains these if you authorize release. If they are missing or destroyed, lay evidence and the statement-of-conditions route (38 CFR 3.302(b)) fill the gap.
- Private treatment records: any civilian diagnosis. A current diagnosis is required for every claim.
- Nexus letter: a clinician's written opinion in VA's probability language, with rationale.
- Lay/buddy statements: written statements from people who served with you (or family), with contact info, describing the event and how the condition changed you. Competent evidence on its own for things like tinnitus, hearing loss, and PTSD symptoms.
- DD-214 and personnel records: dates, MOS, deployments. Deployment orders unlock presumptions.
- Federal records: VA, DoD, and SSA records VA is required to obtain when you identify and authorize them (38 CFR 3.159(c)). For private records, VA must make reasonable efforts if you identify and authorize.
Presumptions remove the nexus burden (38 CFR 3.307-3.309, PACT Act 2022): Agent Orange (Vietnam plus added locations like Thailand bases 1962-1976, and now hypertension and MGUS), Gulf War undiagnosed illness, and burn-pit toxic exposure (respiratory cancers, asthma, COPD, sinusitis, and roughly 20 more conditions for qualifying service locations after August 2, 1990 or September 11, 2001). If a presumption applies, you prove location and dates, not causation.""",
    "status": """What each claim status means
- Claim received: VA logged the claim. The date of receipt (38 CFR 3.1(r)) generally sets the effective date.
- Initial review: a Veterans Service Representative (VSR) checks completeness and requests records. The Development Letter (duty-to-notify under 38 CFR 3.159(b)) may arrive here. You have 1 year to respond; VA can decide after 30 days of silence.
- Evidence gathering, review, and decision: records collected, then a Rating VSR and a Rating Veterans Representative (RVV) rate the claim per the VASRD. Longest phase.
- Preparation for notification: the decision letter is being written.
- Complete: decision letter mailed. It states the rating per condition, the effective date, and back pay.
Flags in the API: documents_needed=true means VA wants something from you. development_letter_sent=true means respond to that letter. decision_letter_sent=true means the decision is out and the 1-year review clock starts.
VA's side of the bargain is the duty to assist (38 CFR 3.159, from the Vets' Claims Assistance Act of 2000): notify you of what is missing, get federal records, make reasonable efforts on private records, and provide a C&P exam or medical opinion when there is an indication of a link. If VA skips any of that, it is appealable error.""",
    "ratings": """How VA ratings and pay work
- Each condition is rated 0-100 percent in 10-point steps under the VA Schedule for Rating Disabilities (38 CFR Part 4). Ratings reflect average impairment of earning capacity (38 CFR 4.1), and diagnostic codes that overlap are not double-rated (38 CFR 4.13, 4.14).
- Combined ratings do not add. Per the combined ratings table (38 CFR 4.25): sort ratings high to low, combine the first two from the table, carry the unrounded value forward, and round only once at the end (final 5 rounds up). Example: 50 plus 30 combines to 65, which rounds to 70. 40 plus 40 combines to 64, which rounds to 60.
- Pay is tax-free and set by the combined rating plus dependents. Allowances for a spouse/children and parents start at 30 percent combined. 100 percent (or TDIU under 38 CFR 4.16) pays the top rate.
- 0 percent ratings still matter: they establish service connection, open VA health care and other perks, and let the rating be raised if the condition worsens.
- Effective dates (38 CFR 3.400): normally the date VA receives the claim, or the date entitlement arose, whichever is later. Big exception: a direct service-connection claim received within 1 year after separation is effective the day after separation. An intent-to-file date carries the effective date back if a complete claim follows within 1 year (38 CFR 3.155(b)). Back pay runs from the effective date.
- If the rating seems low: see the appeals topic. Do not file a brand-new claim; use the decision-review lanes to protect the effective date.""",
    "representatives": """Who may help you, and who may charge you
Federal law (38 USC 5901-5905, 38 CFR Part 14) says anyone who prepares, presents, or prosecutes a VA claim for compensation must be accredited by VA's Office of General Counsel. That covers help even when it is free.
- VSO representatives (DAV, VFW, American Legion, TROC), and state/county service officers: recognized organizations, never allowed to charge a fee.
- Accredited claims agents and attorneys: may charge, but only after the agency of original jurisdiction has issued a decision AND they have filed a power of attorney plus a fee agreement with VA. No upfront fees on initial claims. Fee agreements are filed with VA and reviewed for reasonableness.
- Anyone who is not accredited, or who demands money for an initial claim, is violating federal law. That is the classic claims scammer.
- Verify accreditation at va.gov/ogc/apps/accreditation.
- Appointing a rep: VA Form 21-22 (veteran) or 21-22a (attorney/agent). The Benefits Claims API can submit both.
- File yourself when the claim is simple (one condition, clean records). Get an accredited rep for PTSD/MST, toxic-exposure presumptives, or after a denial.
- Note for tools like this plugin: giving general information is fine; preparing and prosecuting a claim for someone else for a fee requires accreditation. Keep that line in any product pitch.""",
    "timeline": """Deadlines and realistic timelines
Hard deadlines to respect:
- Intent to file: complete claim within 1 year or the date is lost (38 CFR 3.155(b)(4)).
- Post-decision reviews (38 CFR 3.2400, the AMA decision-review lanes): Higher-Level Review (Form 20-0996) and Board Appeal (Form 10182) must be filed within 1 year of the decision letter date. A Supplemental Claim (Form 20-0995, new and relevant evidence) has no deadline, but file within 1 year to keep the effective date chain intact.
- After a denial, pick exactly one lane within that year. HLR: no new evidence, a senior reviewer looks for error. Supplemental: new evidence. Board: Veterans Law Judge, with Direct, Evidence, or Hearing docket options. Miss the year and the decision becomes final; a Supplemental Claim can still reopen, usually with a new effective date.
- Evidence after a Development Letter: 1 year to submit; VA may decide after 30 days of no response.
Typical processing (varies by regional office):
- Fully Developed Claim: often 3-6 months.
- Standard original claim: 6-12+ months.
- Supplemental Claim: 4-8 months. Higher-Level Review: 4-7 months. Board Appeal: many months to years depending on docket.
- API note: submissions via the Claims API take over an hour to show established in sandbox, over two days in production.
Track weekly with va_claim_track (a cron job calling it every Monday morning is the usual setup).""",
    "appeals": """Contesting a decision (Appeals Modernization Act lanes, 38 CFR Part 3 Subpart D and Part 19/20)
Lane 1, Supplemental Claim (Form 20-0995): submit new and relevant evidence (a nexus letter, new records, a worse-condition statement). No filing deadline, but within 1 year of the decision keeps your effective date chain. Can follow any decision, any number of times.
Lane 2, Higher-Level Review (Form 20-0996): no new evidence. A more senior adjudicator reviews for errors like misapplied rating criteria or records VA failed to obtain. Within 1 year. Once per decision; no HLR after an HLR on the same issue.
Lane 3, Board Appeal (Form 10182, the Notice of Disagreement): within 1 year. Choose the Direct Evidence docket (nothing new), Evidence docket (new records), or Hearing docket (testify before a Veterans Law Judge).
After a lane decision you can move up a lane or file a new Supplemental Claim. A Board loss goes to the U.S. Court of Appeals for Veterans Claims.
Effective-date strategy: staying inside the 1-year windows preserves the original effective date, which is where the back pay lives. A new claim after finality restarts the date and costs money.""",
    "programs": """The other VA benefits worth tracking
- Education (Post-9/11 GI Bill, Ch. 33): entitlement runs 18 months to 20 years after last active-duty separation depending on era (forever for many under the Forever GI Bill). Track remaining months/percent and school enrollment. The Lighthouse Education Benefits API returns awarded/used/remaining, but it is restricted to VA and approved partners.
- VA health care (VHA enrollment, Form 10-10EZ): priority groups 1-8 drive copays and wait times. Service-connected veterans get care for that condition free. Re-enrollment and income verification (means test) recur; track the enrollment status and priority group.
- CHAMPVA: health coverage for spouses/dependents of veterans rated permanently 100% disabled, or who died of a service-connected condition, or children of such veterans, when TRICARE is not available. Application is Form 10-10d. Eligibility must be renewed; a rating change or age-26 cutoff ends it. Track the effective/expiration dates.
- Service members' and veterans' life insurance (SGLI/VGLI/VSLI): SGLI ends ~120 days after separation; VGLI conversion must happen within that window with no medical exam. This deadline catches people.
- Pension (wartime, needs income test) and Veteran Readiness & Employment (Chapter 31, VR&E): separate claim tracks.
Track every expiration in the deadlines list; the tracker warns as they approach."""
}


def guide_dict(topic: str = "") -> dict:
    if topic:
        body = GUIDE.get(topic)
        if not body:
            return {"error": f"Unknown topic '{topic}'", "topics": sorted(GUIDE)}
        return {"topic": topic, "text": body}
    return {"topics": sorted(GUIDE), "text": "\n\n".join(GUIDE.values())}
