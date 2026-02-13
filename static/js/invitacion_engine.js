// 1. Countdown Logic
function startCountdown(targetDate) {
    const countdownDate = new Date(targetDate).getTime();

    const timer = setInterval(function () {
        const now = new Date().getTime();
        const distance = countdownDate - now;

        const days = Math.floor(distance / (1000 * 60 * 60 * 24));
        const hours = Math.floor((distance % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60));
        const minutes = Math.floor((distance % (1000 * 60 * 60)) / (1000 * 60));
        const seconds = Math.floor((distance % (1000 * 60)) / 1000);

        document.getElementById("dias").innerHTML = days;
        document.getElementById("horas").innerHTML = hours;
        document.getElementById("minutos").innerHTML = minutes;
        document.getElementById("segundos").innerHTML = seconds;

        if (distance < 0) {
            clearInterval(timer);
            document.getElementById("countdown").innerHTML = "¡ES HOY!";
        }
    }, 1000);
}

// 2. Audio Control
const audio = document.getElementById('bg-music');
const musicBtn = document.getElementById('music-toggle');

musicBtn.addEventListener('click', () => {
    if (audio.paused) {
        audio.play();
        musicBtn.classList.add('playing');
    } else {
        audio.pause();
        musicBtn.classList.remove('playing');
    }
});

// 3. AOS Init (Animaciones al scroll)
AOS.init({
    duration: 1000,
    once: true
});