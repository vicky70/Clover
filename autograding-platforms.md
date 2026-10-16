# Auto-Grading Platforms for Code and Tech Skills

Most of these give **grades only** (pass/fail or a score). A few research tools give **real feedback**.

---

## 1. Indian College Platforms (grade only)

### CodeTantra
- **Link:** https://codetantra.com
- **Intro:** Indian EdTech startup, founded in 2014, recognized by Startup India and selected by MHRD/AICTE. Colleges use it for lab courses (C, C++, DSA, Python, Java) aligned to university syllabi. It gives weekly lab tasks with auto-evaluation, lab record automation, and online coding exams.
- **Covers:** Languages, DSA. No Docker, Linux, or cloud.

### Prutor (IIT Kanpur)
- **Link:** https://cse.iitk.ac.in/users/karkare/prutor/
- **Intro:** Browser-based programming tutor built at IIT Kanpur. It runs labs, assignments, and exams with grading. Also used for AKTU B.Tech labs (in Hindi as well). Teams are adding AI tools for instant feedback and program repair.
- **Covers:** Intro programming (C, Python, Java).

---

## 2. General Code Autograders (score + test logs)

### GitHub Classroom
- **Link:** https://classroom.github.com
- **Intro:** Free GitHub tool for teachers. Tests run on every push using GitHub Actions. Students see a green check or a red X. Teachers can leave feedback on a pull request, but they write it by hand.
- **Covers:** Any language that can run in Linux.

### Gradescope
- **Link:** https://www.gradescope.com
- **Intro:** Grading platform used by universities. It supports code autograders and rubric-based grading, with some AI help.

### CodeHS
- **Link:** https://codehs.com
- **Intro:** Coding platform, mainly for schools. It offers autograding, teacher dashboards, and AI hints for students.

---

## 3. Docker / Linux / Cloud Hands-On Labs (grade only, paid)

### Pluralsight Hands-On Labs
- **Link:** https://www.pluralsight.com/labs
- **Intro:** Sandbox labs for AWS, GCP, Azure, Linux, and more. Each step is validated, and Challenge Mode gives no guidance. Built for individuals and companies.

### Cloud Academy (by QA)
- **Link:** https://cloudacademy.com
- **Intro:** Cloud labs on AWS, Azure, and GCP with validation checks. For example, its "Getting Started with Docker on Linux for AWS" lab verifies each step.

### DigitalCloud Training – Challenge Labs
- **Link:** https://digitalcloud.training/hands-on-challenge-labs/
- **Intro:** Over 1000 scored labs covering AWS, Azure, Linux, and containers. Built on Skillable, the same platform AWS uses for its exam labs.

### Skillable
- **Link:** https://www.skillable.com
- **Intro:** Lab platform that powers many vendor and certification labs, including AWS Exam Labs.

### CloudLabs (by Spektra Systems)
- **Link:** https://cloudlabs.ai
- **Intro:** Hands-on lab platform for companies and universities on Azure, AWS, GCP, and VMs. Auto-grading tells you whether you completed the lab, and isolated environments let you build a lab once and reuse it.

---

## 4. Real Feedback Tools (mostly research, not products)

### Autograder+
- **Link:** https://arxiv.org/abs/2510.26402
- **Intro:** Research framework that turns pass/fail autograding into feedback, using a fine-tuned LLM. It also groups similar student solutions so teachers can see patterns.

### Stanford AI Feedback Tool
- **Link:** https://hai.stanford.edu/news/first-its-kind-stanford-machine-learning-tool-streamlines-student-feedback-process-computer
- **Intro:** AI that gives feedback on intro CS coding homework. Students agreed with it more often than with human feedback.

### Waterloo / NTNU AI Grading Tool
- **Link:** https://uwaterloo.ca/computer-science/news/new-ai-tool-can-make-grading-faster-fairer-stronger
- **Intro:** Humans and AI write feedback together using a smart comment bank. It shows mistakes as a tree diagram. The teacher stays in control.

### Mark My Works (UNSW Canberra)
- **Link:** https://arxiv.org/abs/2601.10093
- **Intro:** Local autograder that combines unit tests with LLM-written explanations and code-quality critique. Piloted on a course of 191 students.

### CodeMentor-AI (Open University Malaysia)
- **Link:** https://jl4d.org/index.php/ejl4d/article/view/1882
- **Intro:** Tool built on GPT-4 that gives instant, adaptive feedback on programming tasks to help students practice before graded tests.

---

## Gap Summary

1. Indian college tools grade **DSA and languages only**. They don't cover Docker, Linux, or cloud.
2. Docker and cloud labs exist, but they're **paid and made for companies**, not for college teachers.
3. Tools that give **real feedback** are mostly research papers, not products.
4. **None** of them solve student consistency. They check work, but they don't keep students coming back.
